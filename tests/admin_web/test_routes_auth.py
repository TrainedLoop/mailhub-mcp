import asyncio
import dataclasses
import logging
import re
import threading
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mailhub_mcp.admin_web import routes as routes_module
from mailhub_mcp.admin_web.routes import (
    Action,
    _UsedStates,
    admin_endpoint,
    admin_routes,
    done_page,
    require_action,
)
from mailhub_mcp.admin_web.session import (
    FLOW_COOKIE,
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    Session,
    Signer,
    csrf_token,
    decode_session,
    is_fresh,
)
from mailhub_mcp.core.errors import AuthExpired
from tests.admin_web.conftest import (
    CLIENT_BASE,
    PUBLIC,
    callback,
    cookie_header,
    cookie_value,
    login,
    make_env,
    start_login,
)

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'self'; "
    "form-action 'self' https://accounts.google.com; frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
}
EXPIRED = "This sign-in link expired, start again"


def _session(env, token):
    return decode_session(Signer(env.ctx.key), token, env.clock(), env.ctx.allowed_emails)


def _csrf(env, token):
    return csrf_token(env.ctx.key, _session(env, token))


def _attrs(header: str) -> dict[str, str]:
    parts = [p.strip() for p in header.split(";")[1:]]
    out = {}
    for p in parts:
        k, _, v = p.partition("=")
        out[k.lower()] = v
    return out


# --- sign-in page ---------------------------------------------------------------------------


def test_admin_without_session_shows_sign_in_link_and_no_accounts(env):
    env.add_account("alice", "alice@example.test")
    resp = env.client.get("/admin")
    assert resp.status_code == 200
    assert 'href="/admin/login"' in resp.text
    assert "alice" not in resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert '<meta name="viewport"' in resp.text
    assert '<link rel="stylesheet" href="/admin/style.css">' in resp.text


def test_style_css_is_served(env):
    resp = env.client.get("/admin/style.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "body" in resp.text


# --- login start ----------------------------------------------------------------------------


def test_login_sets_flow_cookie_and_uses_public_redirect_uri(env):
    resp, flow, query = start_login(env.client)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    assert query["redirect_uri"] == [f"{PUBLIC}/admin/auth/callback"]
    assert "testserver" not in resp.headers["location"]
    assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]
    header = cookie_header(resp, FLOW_COOKIE)
    attrs = _attrs(header)
    assert "httponly" in attrs and "secure" in attrs
    assert attrs["samesite"] == "Lax"
    assert attrs["path"] == "/admin"
    assert attrs["max-age"] == str(FLOW_SECONDS)
    assert flow and query["state"][0] not in flow  # flow cookie is signed, not the bare state
    assert cookie_header(resp, SESSION_COOKIE) is None


@pytest.mark.parametrize(
    "next_, expected",
    [
        ("/admin/link", "/admin/link"),
        ("/admin", "/admin"),
        ("//evil.test", "/admin"),
        ("https://evil.test", "/admin"),
        ("/admin//evil.test", "/admin"),
        ("/admin\\evil", "/admin"),
        ("/adminx", "/admin"),
        ("/other", "/admin"),
        ("/admin/\r\nSet-Cookie: x=1", "/admin"),
        ("/admin/../oauth", "/admin"),
        ("/admin/login", "/admin"),
        ("/admin/login?next=/admin/link", "/admin"),
        ("/admin/auth/callback", "/admin"),
        ("/admin?msg=expired", "/admin?msg=expired"),
    ],
)
def test_login_next_is_limited_to_admin_paths(env, next_, expected):
    _, flow, query = start_login(env.client, next_)
    resp = callback(env.client, flow, query["state"][0])
    assert resp.status_code == 200
    assert f'content="0;url={expected}"' in resp.text


# --- callback -------------------------------------------------------------------------------


def test_callback_without_session_cookie_sets_session_and_meta_refreshes(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert '<meta http-equiv="refresh" content="0;url=/admin">' in resp.text
    assert 'href="/admin"' in resp.text
    header = cookie_header(resp, SESSION_COOKIE)
    attrs = _attrs(header)
    assert "httponly" in attrs and "secure" in attrs
    assert attrs["samesite"] == "Strict"
    assert attrs["path"] == "/admin"
    assert attrs["max-age"] == str(SESSION_SECONDS)
    s = _session(env, cookie_value(resp, SESSION_COOKIE))
    assert s.email == "me@gmail.com"
    assert s.login_at == env.clock() and s.auth_at == env.clock()
    assert s.has_auth_time is False
    call = env.identity.calls[0]
    assert call["redirect_uri"] == f"{PUBLIC}/admin/auth/callback"
    assert call["code"] == "code-1"
    assert any(r.getMessage() == "admin login email=me@gmail.com" for r in caplog.records)
    assert not any("code-1" in r.getMessage() for r in caplog.records)


def test_callback_deletes_flow_cookie(env):
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    header = cookie_header(resp, FLOW_COOKIE)
    attrs = _attrs(header)
    assert attrs["max-age"] == "0"
    assert attrs["path"] == "/admin"


def test_replayed_callback_writes_nothing_a_second_time(env):
    _, flow, query = start_login(env.client)
    first = callback(env.client, flow, query["state"][0])
    assert cookie_value(first, SESSION_COOKIE)
    again = callback(env.client, flow, query["state"][0])
    assert EXPIRED in again.text
    assert cookie_header(again, SESSION_COOKIE) is None
    assert cookie_header(again, FLOW_COOKIE) is not None  # cleared again
    assert len(env.identity.calls) == 1  # the code is not exchanged a second time


def test_callback_without_flow_cookie_is_expired(env):
    _, _, query = start_login(env.client)
    resp = callback(env.client, None, query["state"][0])
    assert EXPIRED in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert cookie_header(resp, FLOW_COOKIE) is not None
    assert env.identity.calls == []


def test_callback_after_flow_lifetime_is_expired(env):
    _, flow, query = start_login(env.client)
    env.clock.t += FLOW_SECONDS
    resp = callback(env.client, flow, query["state"][0])
    assert EXPIRED in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_callback_with_wrong_state_sets_no_session(env):
    _, flow, _ = start_login(env.client)
    resp = callback(env.client, flow, "not-the-state")
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert EXPIRED in resp.text
    assert env.identity.calls == []


def test_callback_with_consent_refused_sets_no_session(env):
    _, flow, query = start_login(env.client)
    env.client.cookies.set(FLOW_COOKIE, flow)
    resp = env.client.get(
        f"/admin/auth/callback?state={query['state'][0]}&error=access_denied<script>",
        follow_redirects=False,
    )
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert "<script>" not in resp.text
    assert env.identity.calls == []


def test_callback_login_error_shows_safe_message(env):
    env.identity.fail = "Google sign-in failed"
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    assert "Google sign-in failed" in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_failed_sign_in_is_logged_with_its_reason_only(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.identity.fail = "Google sign-in failed"
    env.identity.fail_reason = "unverified-email"
    _, flow, query = start_login(env.client)
    callback(env.client, flow, query["state"][0], code="secret-code")
    failed = [r for r in caplog.records if r.getMessage() == "admin login failed reason=unverified-email"]
    assert failed and failed[0].levelno == logging.WARNING
    assert not any("secret-code" in r.getMessage() for r in caplog.records)


def test_cancelled_sign_in_is_logged(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _, flow, query = start_login(env.client)
    env.client.cookies.set(FLOW_COOKIE, flow)
    env.client.get(f"/admin/auth/callback?state={query['state'][0]}&error=access_denied",
                   follow_redirects=False)
    assert any(r.getMessage() == "admin login cancelled" and r.levelno == logging.INFO
               for r in caplog.records)


def test_callback_refuses_owner_not_allowlisted(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.identity.email = "stranger@gmail.com"
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    assert resp.status_code == 403
    assert "This Google account is not allowed" in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None
    refused = [r for r in caplog.records if r.getMessage() == "admin login refused email=stranger@gmail.com"]
    assert refused and refused[0].levelno == logging.WARNING


def test_allowlist_compares_lowercased(env):
    env.ctx.allowed_emails = frozenset({"me@gmail.com"})
    env.identity.email = "Me@Gmail.com"
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    assert _session(env, cookie_value(resp, SESSION_COOKIE)).email == "me@gmail.com"


def test_auth_time_is_used_and_capped_at_now(env):
    token = login(env.client, auth_time=env.clock() - 100)
    s = _session(env, token)
    assert s.auth_at == env.clock() - 100 and s.has_auth_time is True
    token = login(env.client, auth_time=env.clock() + 1000)
    assert _session(env, token).auth_at == env.clock()


def test_reauth_keeps_sid_and_login_at(env):
    first = _session(env, login(env.client))
    env.clock.t += 3600
    # the browser sends the Strict session cookie to /admin/login (same-site), not to the callback
    second = _session(env, login(env.client))
    assert second.sid == first.sid
    assert second.login_at == first.login_at
    assert second.auth_at == env.clock()


def test_reauth_cookie_lifetime_counts_from_first_login(env):
    login(env.client)
    env.clock.t += 3600
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0], code="other")
    assert _attrs(cookie_header(resp, SESSION_COOKIE))["max-age"] == str(SESSION_SECONDS - 3600)


def test_first_login_does_not_ask_for_max_age(env):
    _, _, query = start_login(env.client)
    assert "max_age" not in query


def test_reauth_login_asks_for_max_age(env):
    login(env.client)
    _, _, query = start_login(env.client)
    assert query["max_age"] == [str(REAUTH_SECONDS)]


def test_reauth_with_stale_auth_time_ends_fresh_and_warns(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    login(env.client, auth_time=env.clock())
    env.clock.t += 7200
    s = _session(env, login(env.client, auth_time=env.clock() - 3600))
    assert is_fresh(s, env.clock())
    assert s.auth_at == env.clock() and s.has_auth_time is True
    warns = [r for r in caplog.records if r.getMessage() == "admin reauth: google returned a stale auth_time"]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING


def test_first_login_keeps_old_auth_time(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    s = _session(env, login(env.client, auth_time=env.clock() - 3600))
    assert s.auth_at == env.clock() - 3600
    assert not any("stale auth_time" in r.getMessage() for r in caplog.records)


def test_login_as_another_owner_starts_a_new_session(env):
    env.ctx.allowed_emails = frozenset({"me@gmail.com", "other@gmail.com"})
    first = _session(env, login(env.client))
    second = _session(env, login(env.client, email="other@gmail.com"))
    assert second.email == "other@gmail.com"
    assert second.sid != first.sid


def test_session_expires_after_eight_hours(env):
    env.add_account("alice", "alice@example.test")
    login(env.client)
    env.clock.t += SESSION_SECONDS
    resp = env.client.get("/admin")
    assert 'href="/admin/login"' in resp.text
    assert "alice" not in resp.text


# --- status page ----------------------------------------------------------------------------


def test_status_page_states_and_backfill(env):
    env.add_account("alice", "Alice@Example.test")
    env.add_account("bob", error=AuthExpired("bob", "mailhub-mcp auth"))
    env.add_account("carol", error=RuntimeError("boom secret-token"))
    login(env.client)
    resp = env.client.get("/admin")
    assert resp.status_code == 200
    page = resp.text.replace("<wbr>", "")  # break opportunities inside addresses
    rows = {alias: re.search(rf"<tr>\s*<th scope=\"row\">{alias}<.*?</tr>", page, re.DOTALL).group(0)
            for alias in ("alice", "bob", "carol")}
    assert ">ok<" in rows["alice"] and "Alice@Example.test" in rows["alice"]
    assert "credential expired" in rows["bob"]
    assert ">error<" in rows["carol"]
    assert "boom" not in resp.text
    assert env.ctx.book.get("alice").address == "alice@example.test"
    assert env.ctx.book.get("bob") is None


def test_status_page_flags_a_different_mailbox_and_keeps_the_book(env, caplog):
    """A credential placed by the CLI or by hand for another mailbox than the one on record."""
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.ctx.book.record_linked("alice", "gmail", "alice@example.test", datetime.now(UTC))
    env.add_account("alice", "intruder@example.test")
    env.ctx.book.record_linked("bob", "gmail", "bob@example.test", datetime.now(UTC))
    env.add_account("bob", "Bob@Example.test")  # same mailbox, other case: ok
    login(env.client)
    page = env.client.get("/admin").text.replace("<wbr>", "")
    alice_row = re.search(r"<tr>\s*<th scope=\"row\">alice<.*?</tr>", page, re.DOTALL).group(0)
    bob_row = re.search(r"<tr>\s*<th scope=\"row\">bob<.*?</tr>", page, re.DOTALL).group(0)
    assert '<span class="badge badge-warn">different mailbox</span>' in alice_row
    assert "alice@example.test" in alice_row
    assert '<span class="badge badge-ok">ok</span>' in bob_row
    assert env.ctx.book.get("alice").address == "alice@example.test"
    warned = [r for r in caplog.records if r.getMessage().startswith("admin status: different mailbox alias=alice")]
    assert warned and warned[0].levelno == logging.WARNING


def test_status_page_logs_error_type_and_alias_only(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.add_account("carol", error=RuntimeError("boom secret-token"))
    login(env.client)
    env.client.get("/admin")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "carol" in text and "RuntimeError" in text
    assert "secret-token" not in text


def test_status_page_lists_removed_aliases(env):
    env.ctx.book.record_address("dave", "gmail", "dave@example.test")
    env.ctx.book.record_removed("dave", datetime.now(UTC))
    login(env.client)
    resp = env.client.get("/admin")
    assert "dave" in resp.text and "dave@example.test" in resp.text.replace("<wbr>", "")
    assert 'action="/admin/accounts/dave/relink"' in resp.text


def test_status_page_shows_provider_label_and_readable_dates(env):
    env.add_account("alice", "alice@example.test")
    env.ctx.book.record_linked("alice", "gmail", "alice@example.test", datetime(2026, 9, 1, 12, tzinfo=UTC))
    env.ctx.book.record_linked("dave", "gmail", "dave@example.test", datetime(2026, 8, 1, tzinfo=UTC))
    env.ctx.book.record_removed("dave", datetime(2026, 10, 1, 9, 30, tzinfo=UTC))
    login(env.client)
    page = env.client.get("/admin").text
    assert "gmail</td>" not in page and ">Gmail</td>" in page
    assert '<time datetime="2026-10-01T09:30:00+00:00">1 Oct 2026</time>' in page
    assert '<time datetime="2026-09-01T12:00:00+00:00">1 Sep 2026</time>' in page
    assert "2026-10-01T09:30:00+00:00<" not in page  # the raw ISO only in the attribute
    assert re.search(r'class="badge badge-ok">ok<', page)
    # long addresses wrap after "@" and before ".", never inside a word
    assert '<span class="addr-text">alice@<wbr>example<wbr>.test</span>' in page


def test_style_has_dark_mode_tokens_and_phone_cards():
    css = routes_module.STYLE_CSS
    assert "prefers-color-scheme: dark" in css
    dark = css.split("prefers-color-scheme: dark", 1)[1]
    for token in ("--bg", "--surface", "--text", "--muted", "--border", "--accent", "--ok", "--warn", "--danger"):
        assert token in dark, token
    assert "@media (max-width: 860px)" in css  # cards on phones (and narrow tablets)
    assert "url(" not in css and "@import" not in css  # nothing outside 'self'


def test_status_page_shows_buttons_and_session_age_notice(env):
    env.add_account("alice", "alice@example.test")
    token = login(env.client)
    resp = env.client.get("/admin")
    assert 'href="/admin/link"' in resp.text
    assert 'action="/admin/accounts/alice/relink"' in resp.text
    assert 'href="/admin/accounts/alice/remove"' in resp.text
    assert "session age claim" in resp.text
    assert f'value="{_csrf(env, token)}"' in resp.text
    assert "me@gmail.com" in resp.text
    login(env.client, auth_time=env.clock())
    assert "session age claim" not in env.client.get("/admin").text


def test_status_page_escapes_alias_and_address(env):
    env.add_account("alice", "<script>alert(1)</script>@example.test")
    # an alias that bypassed the usual rule, straight into the registry
    env.providers["<script>x"] = env.providers["alice"]
    env.ctx.registry.upsert("<script>x", "gmail", env.ctx.accounts_dir / "gmail" / "alice.json")
    login(env.client)
    resp = env.client.get("/admin")
    assert "<script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;@example.test" in resp.text.replace("<wbr>", "")
    assert "&lt;script&gt;x" in resp.text


def test_read_only_context_shows_notice_and_no_buttons(tmp_path):
    env = make_env(tmp_path, writable=False)
    env.add_account("alice", "alice@example.test")
    env.ctx.book.record_address("dave", "gmail", "dave@example.test")
    env.ctx.book.record_removed("dave", datetime.now(UTC))
    login(env.client)
    resp = env.client.get("/admin")
    assert "credentials are read-only: linking is disabled" in resp.text
    assert "/admin/link" not in resp.text
    assert "relink" not in resp.text
    assert "/remove" not in resp.text
    assert "alice@example.test" in resp.text.replace("<wbr>", "")


def test_msg_codes_map_to_fixed_texts(env):
    resp = env.client.get("/admin?msg=expired")
    assert EXPIRED in resp.text
    resp = env.client.get("/admin?msg=<script>free text")
    assert "free text" not in resp.text and "<script>" not in resp.text


# --- logout ---------------------------------------------------------------------------------


def test_logout_with_csrf_clears_cookie(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    token = login(env.client)
    resp = env.client.post("/admin/logout", data={"csrf": _csrf(env, token)},
                           headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    attrs = _attrs(cookie_header(resp, SESSION_COOKIE))
    assert attrs["max-age"] == "0" and attrs["path"] == "/admin"
    assert any(r.getMessage() == "admin logout email=me@gmail.com" for r in caplog.records)


def test_logout_also_deletes_the_flow_cookie(env):
    token = login(env.client)
    resp = env.client.post("/admin/logout", data={"csrf": _csrf(env, token)},
                           headers={"Origin": PUBLIC}, follow_redirects=False)
    attrs = _attrs(cookie_header(resp, FLOW_COOKIE))
    assert attrs["max-age"] == "0" and attrs["path"] == "/admin"


def test_logout_without_csrf_is_forbidden(env):
    login(env.client)
    resp = env.client.post("/admin/logout", data={}, headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 403
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_logout_from_other_origin_is_forbidden(env):
    token = login(env.client)
    for origin in ("https://evil.test", CLIENT_BASE):
        resp = env.client.post("/admin/logout", data={"csrf": _csrf(env, token)},
                               headers={"Origin": origin}, follow_redirects=False)
        assert resp.status_code == 403


def test_logout_with_origin_null_and_no_referer_is_forbidden(env):
    """What a browser sends for a form POST from a page served with `Referrer-Policy: no-referrer`:
    `Origin: null` and no Referer. It must stay refused (an opaque origin proves nothing), which is
    why the admin pages use `same-origin`: then the browser sends the real Origin on their forms."""
    token = login(env.client)
    resp = env.client.post("/admin/logout", data={"csrf": _csrf(env, token)},
                           headers={"Origin": "null"}, follow_redirects=False)
    assert resp.status_code == 403
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_admin_pages_let_the_browser_send_their_origin_on_form_posts(env):
    """`no-referrer` makes browsers send `Origin: null` on form POSTs, which same_origin refuses,
    so every admin form would answer 403. `same-origin` keeps the referrer off cross-site requests."""
    resp = env.client.get("/admin")
    assert resp.headers["referrer-policy"] == "same-origin"


def test_logout_get_is_not_allowed(env):
    login(env.client)
    assert env.client.get("/admin/logout").status_code == 405


# --- headers --------------------------------------------------------------------------------


def test_every_admin_response_carries_security_headers(env):
    token = login(env.client)
    responses = [
        env.client.get("/admin"),
        env.client.get("/admin/style.css"),
        env.client.get("/admin/login", follow_redirects=False),
        env.client.get("/admin/auth/callback?state=x&code=y"),
        env.client.post("/admin/logout", data={}, follow_redirects=False),
        env.client.post("/admin/logout", data={"csrf": _csrf(env, token)},
                        headers={"Origin": PUBLIC}, follow_redirects=False),
    ]
    env.client.cookies.clear()
    responses.append(env.client.get("/admin"))
    for resp in responses:
        for name, value in SECURITY_HEADERS.items():
            assert resp.headers.get(name) == value, (resp.request.url, name)


def test_admin_trailing_slash_redirects_to_admin(env):
    resp = env.client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin"
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers.get(name) == value


def test_unexpected_error_returns_page_with_headers(env, monkeypatch):
    def boom():
        raise RuntimeError("secret")

    login(env.client)
    monkeypatch.setattr(env.ctx.registry, "all", boom)
    resp = env.client.get("/admin")
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert resp.headers["x-frame-options"] == "DENY"


# --- helpers for Task 8 ---------------------------------------------------------------------


def _action_client(env):
    async def action(request):
        result = await require_action(request, env.ctx)
        if isinstance(result, Action):
            assert isinstance(result.session, Session)
            return PlainTextResponse(f"ok {result.session.email} {result.form.get('extra', '')}".strip())
        return result

    # The probe goes first: admin_routes ends with a catch-all 404 for every other /admin/ path.
    app = Starlette(routes=[
        Route("/admin/probe/action", admin_endpoint(env.ctx, action, ("POST",)), methods=["POST"]),
    ] + admin_routes(env.ctx))
    client = TestClient(app, base_url=CLIENT_BASE)
    client.identity = env.identity
    return client


def test_require_action_accepts_fresh_session(env):
    client = _action_client(env)
    token = login(client)
    resp = client.post("/admin/probe/action", data={"csrf": _csrf(env, token)},
                       headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 200 and resp.text == "ok me@gmail.com"


def test_require_action_redirects_stale_session_to_login(env):
    client = _action_client(env)
    token = login(client)
    env.clock.t += REAUTH_SECONDS
    resp = client.post("/admin/probe/action", data={"csrf": _csrf(env, token)},
                       headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 303
    loc = urlsplit(resp.headers["location"])
    assert loc.path == "/admin/login"
    assert parse_qs(loc.query) == {"next": ["/admin/probe/action"]}


@pytest.mark.parametrize("case", ["no-session", "no-csrf", "bad-origin", "referer-only-ok"])
def test_require_action_checks(env, case):
    client = _action_client(env)
    token = login(client)
    data = {"csrf": _csrf(env, token)}
    headers = {"Origin": PUBLIC}
    if case == "no-session":
        client.cookies.clear()
    elif case == "no-csrf":
        data = {}
    elif case == "bad-origin":
        headers = {"Origin": "https://evil.test"}
    elif case == "referer-only-ok":
        headers = {"Referer": f"{PUBLIC}/admin/probe/action"}
    resp = client.post("/admin/probe/action", data=data, headers=headers, follow_redirects=False)
    if case == "referer-only-ok":
        assert resp.status_code == 200
    else:
        assert resp.status_code == 403
        assert resp.headers["x-frame-options"] == "DENY"


def test_done_page_escapes_and_meta_refreshes():
    resp = done_page("Linked <b>x</b>", to="/admin/link")
    assert resp.status_code == 200
    body = resp.body.decode()
    assert '<meta http-equiv="refresh" content="0;url=/admin/link">' in body
    assert 'href="/admin/link"' in body
    assert "&lt;b&gt;" in body
    assert done_page("x", to="//evil.test").body.decode().count("url=/admin\"") == 1


# --- review fixes ---------------------------------------------------------------------------


def test_used_states_prunes_expired_entries_from_the_front():
    store = _UsedStates()
    assert store.claim("a", 0.0)
    assert store.claim("b", 1.0)
    assert not store.claim("a", 2.0)  # still inside its lifetime
    assert store.claim("c", FLOW_SECONDS + 0.5)  # "a" has expired and is pruned
    assert len(store) == 2  # "b" and "c"
    assert store.claim("a", FLOW_SECONDS + 0.5)  # a pruned state can be claimed again


def test_used_states_evicts_oldest_beyond_the_cap():
    store = _UsedStates(cap=3)
    assert all(store.claim(s, 0.0) for s in ("a", "b", "c", "d", "e"))
    assert len(store) == 3  # memory stays bounded
    assert store.claim("f", 1.0)  # the newest claim always succeeds: the owner is never locked out
    assert len(store) == 3
    assert not store.claim("f", 1.0)  # a remembered state is still refused
    assert store.claim("a", 1.0)  # an evicted one is forgotten (the code exchange refuses it)


def test_replay_of_an_evicted_login_state_fails_at_the_code_exchange(env, monkeypatch):
    monkeypatch.setattr(routes_module, "USED_STATES_CAP", 1)
    client = TestClient(Starlette(routes=admin_routes(env.ctx)), base_url=CLIENT_BASE)
    client.identity = env.identity
    _, flow, query = start_login(client)
    first = callback(client, flow, query["state"][0], code="once")
    assert cookie_value(first, SESSION_COOKIE)
    _, flow2, query2 = start_login(client)
    assert cookie_value(callback(client, flow2, query2["state"][0], code="twice"), SESSION_COOKIE)
    # the first state was evicted; its replay reaches Google, which refuses the used code
    again = callback(client, flow, query["state"][0], code="once")
    assert cookie_header(again, SESSION_COOKIE) is None
    assert "Google sign-in failed" in again.text
    assert [c["code"] for c in env.identity.calls] == ["once", "twice", "once"]


def test_login_callback_that_raises_still_deletes_flow_cookie(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret")

    monkeypatch.setattr(env.identity, "complete", boom)
    _, flow, query = start_login(env.client)
    resp = callback(env.client, flow, query["state"][0])
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert _attrs(cookie_header(resp, FLOW_COOKIE))["max-age"] == "0"
    assert resp.headers["x-frame-options"] == "DENY"


def _form_must_not_be_read(monkeypatch):
    async def form(self, *args, **kwargs):
        raise AssertionError("form read before the session and origin checks")

    monkeypatch.setattr(Request, "form", form)


def test_logout_without_session_is_refused_before_reading_the_form(env, monkeypatch):
    _form_must_not_be_read(monkeypatch)
    resp = env.client.post("/admin/logout", data={"csrf": "x"}, headers={"Origin": PUBLIC},
                           follow_redirects=False)
    assert resp.status_code == 403


def test_logout_from_other_origin_is_refused_before_reading_the_form(env, monkeypatch):
    login(env.client)
    _form_must_not_be_read(monkeypatch)
    resp = env.client.post("/admin/logout", data={"csrf": "x"}, headers={"Origin": "https://evil.test"},
                           follow_redirects=False)
    assert resp.status_code == 403


def test_require_action_refuses_before_reading_the_form(env, monkeypatch):
    client = _action_client(env)
    _form_must_not_be_read(monkeypatch)
    resp = client.post("/admin/probe/action", data={"csrf": "x"}, headers={"Origin": PUBLIC},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_status_page_runs_book_io_off_the_event_loop(env, monkeypatch):
    env.add_account("alice", "alice@example.test")
    env.add_account("bob", error=AuthExpired("bob", "mailhub-mcp auth"))
    login(env.client)
    calls = []

    def spy(name):
        real = getattr(env.ctx.book, name)

        def wrapper(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            calls.append((name, on_loop))
            return real(*args, **kwargs)

        monkeypatch.setattr(env.ctx.book, name, wrapper)

    for name in ("record_address", "get", "entries"):
        spy(name)
    assert env.client.get("/admin").status_code == 200
    assert {n for n, _ in calls} == {"record_address", "get", "entries"}
    assert not any(on_loop for _, on_loop in calls)


def test_status_check_times_out_as_error(env, monkeypatch):
    release = threading.Event()

    class Blocking:
        def address(self):
            release.wait(5)
            return "slow@example.test"

    monkeypatch.setattr(routes_module, "STATUS_CHECK_SECONDS", 0.05)
    env.add_account("alice", "alice@example.test")
    env.add_account("slow")
    env.providers["slow"] = Blocking()
    login(env.client)
    try:
        started = time.monotonic()
        resp = env.client.get("/admin")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2
    row = re.search(r"<tr>\s*<th scope=\"row\">slow<.*?</tr>", resp.text, re.DOTALL).group(0)
    assert ">error<" in row
    assert ">ok<" in re.search(r"<tr>\s*<th scope=\"row\">alice<.*?</tr>", resp.text, re.DOTALL).group(0)


def test_allowed_emails_are_lowercased_once(tmp_path):
    env = make_env(tmp_path)
    ctx = dataclasses.replace(env.ctx, allowed_emails=frozenset({"Me@Gmail.COM"}))
    assert ctx.allowed_emails == frozenset({"me@gmail.com"})
    client = TestClient(Starlette(routes=admin_routes(ctx)), base_url=CLIENT_BASE)
    client.identity = env.identity
    env.identity.email = "ME@gmail.com"
    token = login(client, email="ME@gmail.com")
    assert decode_session(Signer(ctx.key), token, env.clock(), ctx.allowed_emails).email == "me@gmail.com"
    client.cookies.set(SESSION_COOKIE, token)
    assert "Sign out" in client.get("/admin").text
