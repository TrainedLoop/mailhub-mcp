import logging
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mailhub_mcp.admin_web.routes import (
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
    "referrer-policy": "no-referrer",
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
    rows = {alias: re.search(rf"<tr>\s*<td>{alias}</td>.*?</tr>", resp.text, re.DOTALL).group(0)
            for alias in ("alice", "bob", "carol")}
    assert ">ok<" in rows["alice"] and "Alice@Example.test" in rows["alice"]
    assert "credential expired" in rows["bob"]
    assert ">error<" in rows["carol"]
    assert "boom" not in resp.text
    assert env.ctx.book.get("alice").address == "alice@example.test"
    assert env.ctx.book.get("bob") is None


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
    assert "dave" in resp.text and "dave@example.test" in resp.text
    assert 'action="/admin/accounts/dave/relink"' in resp.text


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
    assert "&lt;script&gt;alert(1)&lt;/script&gt;@example.test" in resp.text
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
    assert "alice@example.test" in resp.text


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
        form = await request.form()
        result = require_action(request, env.ctx, form)
        if isinstance(result, Session):
            return PlainTextResponse(f"ok {result.email}")
        return result

    app = Starlette(routes=admin_routes(env.ctx) + [
        Route("/admin/accounts/alice/remove", admin_endpoint(env.ctx, action, ("POST",)), methods=["POST"]),
    ])
    client = TestClient(app, base_url=CLIENT_BASE)
    client.identity = env.identity
    return client


def test_require_action_accepts_fresh_session(env):
    client = _action_client(env)
    token = login(client)
    resp = client.post("/admin/accounts/alice/remove", data={"csrf": _csrf(env, token)},
                       headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 200 and resp.text == "ok me@gmail.com"


def test_require_action_redirects_stale_session_to_login(env):
    client = _action_client(env)
    token = login(client)
    env.clock.t += REAUTH_SECONDS
    resp = client.post("/admin/accounts/alice/remove", data={"csrf": _csrf(env, token)},
                       headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 303
    loc = urlsplit(resp.headers["location"])
    assert loc.path == "/admin/login"
    assert parse_qs(loc.query) == {"next": ["/admin/accounts/alice/remove"]}


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
        headers = {"Referer": f"{PUBLIC}/admin/accounts/alice/remove"}
    resp = client.post("/admin/accounts/alice/remove", data=data, headers=headers, follow_redirects=False)
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
