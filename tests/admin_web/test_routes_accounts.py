"""Link, re-link and remove mailboxes from /admin (Task 8)."""

import json
import logging
import re
import stat
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from mailhub_mcp.admin_web import accounts_routes as accounts_module
from mailhub_mcp.admin_web import routes as routes_module
from mailhub_mcp.admin_web.session import (
    FLOW_COOKIE,
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_COOKIE,
    Signer,
    csrf_token,
    decode_flow,
    decode_session,
)
from mailhub_mcp.providers.base import LinkError, LinkResult
from tests.admin_web.conftest import (
    PUBLIC,
    cookie_header,
    cookie_value,
    login,
    make_env,
    start_login,
)

EXPIRED = "This sign-in link expired, start again"
PERMISSIONS = "https://myaccount.google.com/permissions"
CRED = json.dumps({"refresh_token": "secret-refresh", "token": "secret-access"})


def _csrf(env, token):
    s = decode_session(Signer(env.ctx.key), token, env.clock(), env.ctx.allowed_emails)
    return csrf_token(env.ctx.key, s)


def _signed_in(env):
    token = login(env.client)
    env.token = token
    return token


def _post(env, path, data=None, origin=PUBLIC, csrf=True):
    body = dict(data or {})
    if csrf:
        body["csrf"] = _csrf(env, env.token)
    headers = {"Origin": origin} if origin else {}
    env.client.cookies.clear()
    env.client.cookies.set(SESSION_COOKIE, env.token)
    return env.client.post(path, data=body, headers=headers, follow_redirects=False)


def _start_link(env, alias="pessoal", provider="gmail"):
    return _post(env, "/admin/link", {"alias": alias, "provider": provider})


def _flow_and_state(resp):
    flow = cookie_value(resp, FLOW_COOKIE)
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    return flow, query["state"][0]


def _oauth_callback(env, flow, state, code="link-code", **extra):
    """Google's return: a cross-site navigation, so only the Lax flow cookie comes along."""
    env.client.cookies.clear()
    if flow is not None:
        env.client.cookies.set(FLOW_COOKIE, flow)
    params = {"state": state, "code": code, **extra}
    return env.client.get(f"/admin/oauth/callback?{urlencode(params)}", follow_redirects=False)


def _link(env, alias="pessoal", address="box@example.test"):
    env.linker.result = LinkResult(credential_json=CRED, address=address)
    flow, state = _flow_and_state(_start_link(env, alias))
    return _oauth_callback(env, flow, state)


def _cred_path(env, alias):
    return env.ctx.accounts_dir / "gmail" / f"{alias}.json"


def _msg(resp):
    assert resp.status_code == 303, resp.text
    return parse_qs(urlsplit(resp.headers["location"]).query).get("msg", [None])[0]


# --- link form ------------------------------------------------------------------------------


def test_link_form_has_alias_provider_and_csrf(env):
    token = _signed_in(env)
    resp = env.client.get("/admin/link")
    assert resp.status_code == 200
    assert 'name="alias"' in resp.text
    assert 'name="provider"' in resp.text and '<option value="gmail">Gmail</option>' in resp.text
    assert f'value="{_csrf(env, token)}"' in resp.text
    assert 'action="/admin/link"' in resp.text
    assert resp.headers["x-frame-options"] == "DENY"


def test_link_form_without_session_redirects(env):
    resp = env.client.get("/admin/link", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin"


def test_link_form_shows_message_codes(env):
    _signed_in(env)
    resp = env.client.get("/admin/link?msg=alias-reserved")
    assert "reserved" in resp.text


# --- link start -----------------------------------------------------------------------------


def test_link_start_sets_flow_and_redirects_to_google(env):
    _signed_in(env)
    resp = _start_link(env, "pessoal")
    assert resp.status_code == 303
    loc = urlsplit(resp.headers["location"])
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    query = parse_qs(loc.query)
    assert query["redirect_uri"] == [f"{PUBLIC}/admin/oauth/callback"]
    header = cookie_header(resp, FLOW_COOKIE)
    assert "samesite=lax" in header.lower() and "httponly" in header.lower() and "secure" in header.lower()
    flow = decode_flow(Signer(env.ctx.key), cookie_value(resp, FLOW_COOKIE), env.clock())
    assert flow.kind == "link" and flow.alias == "pessoal" and flow.provider == "gmail"
    assert flow.email == "me@gmail.com" and flow.sid
    assert flow.state == query["state"][0]
    assert query["code_challenge"][0] and query["code_challenge"][0] != flow.code_verifier


def test_link_alias_is_stripped_and_lowercased(env):
    _signed_in(env)
    resp = _start_link(env, "Pessoal ")
    assert resp.status_code == 303 and resp.headers["location"].startswith("https://accounts.google.com/")
    flow = decode_flow(Signer(env.ctx.key), cookie_value(resp, FLOW_COOKIE), env.clock())
    assert flow.alias == "pessoal"


@pytest.mark.parametrize(
    "alias, code, text",
    [
        ("Global", "alias-reserved", "reserved"),
        ("bad_alias!", "alias-invalid", "Invalid alias"),
        ("", "alias-invalid", "Invalid alias"),
        ("a" * 33, "alias-invalid", "Invalid alias"),
    ],
)
def test_link_alias_rule(env, alias, code, text):
    _signed_in(env)
    resp = _start_link(env, alias)
    assert _msg(resp) == code
    assert resp.headers["location"].startswith("/admin/link?")
    assert cookie_header(resp, FLOW_COOKIE) is None
    page = env.client.get(resp.headers["location"])
    assert text in page.text


def test_link_alias_already_linked_is_refused(env):
    env.add_account("pessoal", "box@example.test")
    _signed_in(env)
    resp = _start_link(env, "pessoal")
    assert _msg(resp) == "alias-taken"
    assert cookie_header(resp, FLOW_COOKIE) is None


def test_link_alias_recorded_as_removed_points_to_relink(env):
    env.ctx.book.record_address("pessoal", "gmail", "box@example.test")
    env.ctx.book.record_removed("pessoal", datetime.now(UTC))
    _signed_in(env)
    resp = _start_link(env, "pessoal")
    assert _msg(resp) == "alias-removed"
    assert "re-link" in env.client.get(resp.headers["location"]).text
    assert cookie_header(resp, FLOW_COOKIE) is None


def test_link_unknown_provider_is_refused(env):
    _signed_in(env)
    resp = _start_link(env, "pessoal", provider="outlook")
    assert _msg(resp) == "provider-invalid"
    assert cookie_header(resp, FLOW_COOKIE) is None


# --- link callback --------------------------------------------------------------------------


def test_link_happy_path(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _signed_in(env)
    resp = _link(env, "pessoal", "Box@Example.test")
    assert resp.status_code == 200, resp.text
    assert "location" not in resp.headers
    assert '<meta http-equiv="refresh" content="0;url=/admin">' in resp.text
    assert "Linked pessoal (box@example.test)" in resp.text
    path = _cred_path(env, "pessoal")
    assert path.read_text() == CRED
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "pessoal" in env.ctx.registry.aliases()
    assert env.ctx.registry.get("pessoal").credential_path == path
    entry = env.ctx.book.get("pessoal")
    assert entry.address == "box@example.test" and entry.provider == "gmail"
    assert entry.linked_at and entry.removed_at is None
    assert env.linker.calls[0]["redirect_uri"] == f"{PUBLIC}/admin/oauth/callback"
    assert env.linker.calls[0]["code"] == "link-code"
    assert _attrs_max_age(cookie_header(resp, FLOW_COOKIE)) == "0"
    messages = [r.getMessage() for r in caplog.records]
    assert "admin link alias=pessoal provider=gmail address=box@example.test by=me@gmail.com" in messages
    joined = "\n".join(messages)
    assert "secret-refresh" not in joined and "link-code" not in joined


def _attrs_max_age(header):
    for part in header.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "max-age":
            return v
    return None


def test_link_callback_ignores_session_cookie_and_uses_flow_only(env):
    """Review Focus 1: no session cookie arrives with Google's return; the flow cookie is enough."""
    _signed_in(env)
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, flow, state)
    assert resp.status_code == 200
    assert "pessoal" in env.ctx.registry.aliases()


def test_replayed_link_callback_writes_nothing_a_second_time(env, monkeypatch):
    """Review Focus 2."""
    _signed_in(env)
    env.linker.result = LinkResult(credential_json=CRED, address="box@example.test")
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    assert _oauth_callback(env, flow, state).status_code == 200
    writes = []
    monkeypatch.setattr(accounts_module, "write_credential", lambda *a, **k: writes.append(a))
    again = _oauth_callback(env, flow, state)
    assert EXPIRED in again.text
    assert len(env.linker.calls) == 1
    assert writes == []
    assert _attrs_max_age(cookie_header(again, FLOW_COOKIE)) == "0"


def test_link_callback_without_flow_cookie_is_expired(env):
    _signed_in(env)
    _, state = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, None, state)
    assert EXPIRED in resp.text
    assert env.linker.calls == []


def test_link_callback_with_wrong_state_is_expired(env):
    _signed_in(env)
    flow, _ = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, flow, "other-state")
    assert EXPIRED in resp.text
    assert env.linker.calls == []


def test_link_callback_after_flow_lifetime_is_expired(env):
    _signed_in(env)
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    env.clock.t += FLOW_SECONDS
    resp = _oauth_callback(env, flow, state)
    assert EXPIRED in resp.text
    assert env.linker.calls == []


def test_login_flow_cookie_is_not_accepted_by_link_callback(env):
    _, flow, query = start_login(env.client)
    resp = _oauth_callback(env, flow, query["state"][0])
    assert EXPIRED in resp.text
    assert env.linker.calls == []


def test_link_callback_refuses_owner_removed_from_allowlist(env):
    _signed_in(env)
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    env.ctx.allowed_emails = frozenset({"other@gmail.com"})
    resp = _oauth_callback(env, flow, state)
    assert resp.status_code == 403
    assert env.linker.calls == []
    assert not _cred_path(env, "pessoal").exists()


def test_consent_refused_writes_nothing(env):
    _signed_in(env)
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, flow, state, code="", error="access_denied")
    assert "consent refused" in resp.text.lower() or "consent was refused" in resp.text
    assert env.linker.calls == []
    assert not _cred_path(env, "pessoal").exists()
    assert env.ctx.registry.aliases() == []
    assert env.ctx.book.get("pessoal") is None


def test_link_error_message_is_shown_and_nothing_written(env):
    _signed_in(env)
    env.linker.error = LinkError("Gmail access was not granted")
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, flow, state)
    assert resp.status_code == 400
    assert "Gmail access was not granted" in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert env.ctx.book.get("pessoal") is None


def test_link_callback_that_raises_still_deletes_flow_cookie(env):
    _signed_in(env)
    env.linker.error = RuntimeError("secret-boom")
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    resp = _oauth_callback(env, flow, state)
    assert resp.status_code == 500
    assert "secret-boom" not in resp.text
    assert _attrs_max_age(cookie_header(resp, FLOW_COOKIE)) == "0"
    assert resp.headers["x-frame-options"] == "DENY"


def test_address_already_linked_under_another_alias_is_refused(env):
    env.add_account("work", "box@example.test")
    env.ctx.book.record_linked("work", "gmail", "box@example.test", datetime.now(UTC))
    _signed_in(env)
    resp = _link(env, "pessoal", "BOX@example.test")
    assert resp.status_code == 400
    assert "already linked under another alias" in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    assert env.ctx.book.get("pessoal") is None


def test_alias_taken_in_the_meantime_is_refused(env):
    _signed_in(env)
    env.linker.result = LinkResult(credential_json=CRED, address="box@example.test")
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    env.add_account("pessoal", "other@example.test")  # linked from another tab meanwhile
    before = _cred_path(env, "pessoal").read_text()
    resp = _oauth_callback(env, flow, state)
    assert resp.status_code == 400
    assert "alias is already taken" in resp.text
    assert _cred_path(env, "pessoal").read_text() == before
    assert env.ctx.book.get("pessoal") is None


def test_link_callback_escapes_values(env):
    _signed_in(env)
    resp = _link(env, "pessoal", "<script>x</script>@example.test")
    assert "<script>" not in resp.text


# --- re-link --------------------------------------------------------------------------------


def _relink(env, alias, address):
    env.linker.result = LinkResult(credential_json=CRED, address=address)
    resp = _post(env, f"/admin/accounts/{alias}/relink")
    assert resp.status_code == 303, resp.text
    flow, state = _flow_and_state(resp)
    return _oauth_callback(env, flow, state)


def test_relink_same_mailbox_case_insensitive(env, caplog):
    """Review Focus 4."""
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    old_account = env.ctx.registry.get("pessoal")
    _signed_in(env)
    resp = _relink(env, "pessoal", "Me@Gmail.com")
    assert resp.status_code == 200, resp.text
    assert _cred_path(env, "pessoal").read_text() == CRED
    assert env.ctx.registry.get("pessoal") is not old_account  # cached provider dropped
    assert env.ctx.book.get("pessoal").address == "me@gmail.com"
    assert any(r.getMessage() == "admin relink alias=pessoal provider=gmail address=me@gmail.com by=me@gmail.com"
               for r in caplog.records)


def test_relink_flow_cookie_kind_and_provider_from_record(env):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    resp = _post(env, "/admin/accounts/pessoal/relink")
    flow = decode_flow(Signer(env.ctx.key), cookie_value(resp, FLOW_COOKIE), env.clock())
    assert flow.kind == "relink" and flow.alias == "pessoal" and flow.provider == "gmail"


def test_relink_different_mailbox_is_refused_and_file_unchanged(env):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    before = _cred_path(env, "pessoal").read_text()
    _signed_in(env)
    resp = _relink(env, "pessoal", "other@gmail.com")
    assert resp.status_code == 400
    assert "That is a different mailbox than pessoal (expected me@gmail.com)" in resp.text
    assert _cred_path(env, "pessoal").read_text() == before
    assert env.ctx.book.get("pessoal").address == "me@gmail.com"


def test_relink_without_recorded_address_is_accepted_once(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.add_account("pessoal")
    _signed_in(env)
    resp = _relink(env, "pessoal", "Me@Gmail.com")
    assert resp.status_code == 200, resp.text
    assert env.ctx.book.get("pessoal").address == "me@gmail.com"
    assert any(r.getMessage().startswith("admin relink without recorded address alias=pessoal")
               for r in caplog.records)
    # from now on the address is checked
    resp = _relink(env, "pessoal", "other@gmail.com")
    assert resp.status_code == 400


def test_relink_removed_alias_restores_it(env):
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    env.ctx.book.record_removed("pessoal", datetime.now(UTC))
    _signed_in(env)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 200, resp.text
    assert "pessoal" in env.ctx.registry.aliases()
    assert env.ctx.book.get("pessoal").removed_at is None


def test_relink_address_linked_under_another_alias_is_refused(env):
    env.add_account("work", "me@gmail.com")
    env.ctx.book.record_linked("work", "gmail", "me@gmail.com", datetime.now(UTC))
    env.add_account("pessoal")
    before = _cred_path(env, "pessoal").read_text()
    _signed_in(env)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 400
    assert "already linked under another alias" in resp.text
    assert _cred_path(env, "pessoal").read_text() == before


def test_relink_unknown_alias_is_refused(env):
    _signed_in(env)
    resp = _post(env, "/admin/accounts/nobody/relink")
    assert _msg(resp) == "unknown-alias"
    assert cookie_header(resp, FLOW_COOKIE) is None


def test_relink_stale_session_goes_through_google_back_to_admin(env):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    env.clock.t += REAUTH_SECONDS
    resp = _post(env, "/admin/accounts/pessoal/relink")
    assert resp.status_code == 303
    loc = urlsplit(resp.headers["location"])
    assert loc.path == "/admin/login" and parse_qs(loc.query) == {"next": ["/admin"]}


# --- action gate ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/admin/link", "/admin/accounts/pessoal/relink", "/admin/accounts/pessoal/remove"],
)
def test_actions_without_csrf_or_from_another_origin_are_forbidden(env, path):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    data = {"alias": "new", "provider": "gmail", "confirm": "pessoal"}
    assert _post(env, path, data, csrf=False).status_code == 403
    assert _post(env, path, data, origin="https://evil.test").status_code == 403
    env.client.cookies.clear()
    resp = env.client.post(path, data=data, headers={"Origin": PUBLIC}, follow_redirects=False)
    assert resp.status_code == 403
    assert _cred_path(env, "pessoal").exists() and env.linker.revoked == []


@pytest.mark.parametrize(
    "path, next_",
    [
        ("/admin/link", "/admin/link"),
        ("/admin/accounts/pessoal/remove", "/admin/accounts/pessoal/remove"),
    ],
)
def test_actions_with_stale_session_reauthenticate(env, path, next_):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    env.clock.t += 6 * 60
    resp = _post(env, path, {"alias": "new", "provider": "gmail", "confirm": "pessoal"})
    assert resp.status_code == 303
    loc = urlsplit(resp.headers["location"])
    assert loc.path == "/admin/login" and parse_qs(loc.query) == {"next": [next_]}
    assert cookie_header(resp, FLOW_COOKIE) is None
    assert _cred_path(env, "pessoal").exists() and env.linker.revoked == []


# --- remove ---------------------------------------------------------------------------------


def _journal_change(env, account, change_id):
    with env.ctx.journal.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"change_id": change_id, "account": account, "ids": ["m1"],
                            "added": ["x"], "removed": [], "at": "2026-10-31T00:00:00+00:00"}) + "\n")


def test_remove_confirmation_shows_address_and_undoable_count(env):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    _journal_change(env, "pessoal", "c1")
    _journal_change(env, "pessoal", "c2")
    _journal_change(env, "work", "c3")
    assert env.ctx.journal.undoable_count("pessoal") == 2
    token = _signed_in(env)
    resp = env.client.get("/admin/accounts/pessoal/remove")
    assert resp.status_code == 200
    assert "me@gmail.com" in resp.text
    assert "<strong>2</strong>" in resp.text
    assert "same alias" in resp.text
    assert 'name="confirm"' in resp.text
    assert f'value="{_csrf(env, token)}"' in resp.text
    assert 'action="/admin/accounts/pessoal/remove"' in resp.text


def test_remove_confirmation_for_unknown_alias_redirects(env):
    _signed_in(env)
    resp = env.client.get("/admin/accounts/nobody/remove", follow_redirects=False)
    assert _msg(resp) == "not-linked"


def test_remove_with_wrong_typed_alias_is_refused(env):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    resp = _post(env, "/admin/accounts/pessoal/remove", {"confirm": "work"})
    assert _msg(resp) == "confirm-mismatch"
    assert resp.headers["location"].startswith("/admin/accounts/pessoal/remove?")
    assert _cred_path(env, "pessoal").exists()
    assert env.linker.revoked == []
    assert "pessoal" in env.ctx.registry.aliases()


def test_remove_revokes_deletes_and_records(env, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    path = _cred_path(env, "pessoal")
    _signed_in(env)
    resp = _post(env, "/admin/accounts/pessoal/remove", {"confirm": " Pessoal "})
    assert resp.status_code == 200, resp.text
    assert env.linker.revoked == [path]
    assert not path.exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    entry = env.ctx.book.get("pessoal")
    assert entry.address == "me@gmail.com" and entry.removed_at
    assert PERMISSIONS not in resp.text
    assert any(r.getMessage() == "admin remove alias=pessoal provider=gmail address=me@gmail.com by=me@gmail.com"
               for r in caplog.records)


def test_remove_when_revoke_fails_still_removes_and_links_to_permissions(env):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    env.linker.revoke_result = False
    _signed_in(env)
    resp = _post(env, "/admin/accounts/pessoal/remove", {"confirm": "pessoal"})
    assert resp.status_code == 200
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    assert env.ctx.book.get("pessoal").removed_at
    assert f'href="{PERMISSIONS}"' in resp.text
    assert "http-equiv" not in resp.text  # stays on screen so the link can be followed


def test_remove_unknown_alias_is_refused(env):
    _signed_in(env)
    resp = _post(env, "/admin/accounts/nobody/remove", {"confirm": "nobody"})
    assert _msg(resp) == "not-linked"
    assert env.linker.revoked == []


def test_removed_alias_cannot_be_linked_again_as_new(env):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    _signed_in(env)
    _post(env, "/admin/accounts/pessoal/remove", {"confirm": "pessoal"})
    assert _msg(_start_link(env, "pessoal")) == "alias-removed"


# --- read-only ------------------------------------------------------------------------------


def test_read_only_context_changes_nothing(tmp_path):
    env = make_env(tmp_path, writable=False)
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    before = _cred_path(env, "pessoal").read_text()
    book_before = env.ctx.book.path.read_text()
    _signed_in(env)
    for path, data in [
        ("/admin/link", {"alias": "new", "provider": "gmail"}),
        ("/admin/accounts/pessoal/relink", {}),
        ("/admin/accounts/pessoal/remove", {"confirm": "pessoal"}),
    ]:
        resp = _post(env, path, data)
        assert _msg(resp) == "read-only", path
        assert cookie_header(resp, FLOW_COOKIE) is None
    assert env.client.get("/admin/link", follow_redirects=False).status_code == 303
    assert _cred_path(env, "pessoal").read_text() == before
    assert env.ctx.book.path.read_text() == book_before
    assert env.ctx.registry.aliases() == ["pessoal"]
    assert env.linker.revoked == []


def test_read_only_at_callback_writes_nothing(env):
    _signed_in(env)
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    env.ctx.writable = False
    resp = _oauth_callback(env, flow, state)
    assert "read-only" in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert env.linker.calls == []


# --- single-use store -----------------------------------------------------------------------


def test_replay_of_an_evicted_link_state_fails_at_the_code_exchange(env, monkeypatch):
    monkeypatch.setattr(routes_module, "USED_STATES_CAP", 2)
    second = env.ctx.accounts_dir.parent / "evict"
    second.mkdir()
    env = make_env(second)
    _signed_in(env)
    env.linker.result = LinkResult(credential_json=CRED, address="box@example.test")
    flow, state = _flow_and_state(_start_link(env, "pessoal"))
    assert _oauth_callback(env, flow, state).status_code == 200
    for alias in ("other1", "other2"):  # two more consumed states push the first one out
        f, st = _flow_and_state(_start_link(env, alias))
        assert "consent" in _oauth_callback(env, f, st, code="", error="access_denied").text.lower()
    writes = []
    monkeypatch.setattr(accounts_module, "write_credential", lambda *a, **k: writes.append(a))
    again = _oauth_callback(env, flow, state)
    assert again.status_code == 400
    assert "Google refused the authorization code" in again.text
    assert [c["code"] for c in env.linker.calls] == ["link-code", "link-code"]
    assert writes == []


def test_remove_of_an_account_the_book_never_saw_leaves_a_record(env):
    env.add_account("legacy")  # linked before the account book existed; address never recorded
    assert env.ctx.book.get("legacy") is None
    _signed_in(env)
    resp = _post(env, "/admin/accounts/legacy/remove", {"confirm": "legacy"})
    assert resp.status_code == 200, resp.text
    entry = env.ctx.book.get("legacy")
    assert entry.provider == "gmail" and entry.address is None and entry.removed_at
    assert _msg(_start_link(env, "legacy")) == "alias-removed"
    resp = _relink(env, "legacy", "Any@Gmail.com")  # once, without the address check
    assert resp.status_code == 200, resp.text
    assert env.ctx.book.get("legacy").address == "any@gmail.com"
    assert env.ctx.book.get("legacy").removed_at is None


def test_full_login_store_does_not_block_linking(env, monkeypatch):
    monkeypatch.setattr(routes_module, "USED_STATES_CAP", 1)
    second = env.ctx.accounts_dir.parent / "second"
    second.mkdir()
    env2 = make_env(second)
    _signed_in(env2)  # uses the only login slot
    resp = _link(env2, "pessoal")
    assert resp.status_code == 200, resp.text


def test_headers_on_every_account_response(env):
    env.add_account("pessoal", "me@gmail.com")
    _signed_in(env)
    responses = [
        env.client.get("/admin/link"),
        env.client.get("/admin/accounts/pessoal/remove"),
        _start_link(env, "new"),
        _post(env, "/admin/accounts/pessoal/relink"),
        _oauth_callback(env, None, "x"),
    ]
    for resp in responses:
        assert resp.headers["content-security-policy"].startswith("default-src 'none'")
        assert resp.headers["cache-control"] == "no-store"


# --- Task 8b: no-address note, rollback, Google permissions hint -----------------------------

GRANT_HINT = ("Google still lists mailhub as allowed for that account; you can remove it at "
              "https://myaccount.google.com/permissions.")
NO_ADDRESS_NOTE = "No address on record: this re-link will accept any mailbox once."


def _visible(html):
    """Page text without tags, whitespace collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split()).replace(" .", ".")


def test_relink_control_without_address_shows_one_time_note(env):
    env.add_account("pessoal")  # no address on record, and the status check reports none
    env.providers["pessoal"]._address = None
    env.ctx.book.record_removed("legacy", datetime.now(UTC), provider="gmail")  # removed, no address
    env.add_account("work", "work@example.test")
    _signed_in(env)
    page = env.client.get("/admin").text
    assert page.count(NO_ADDRESS_NOTE) == 2
    work_row = re.search(r"<tr>\s*<th scope=\"row\">work<.*?</tr>", page, re.DOTALL).group(0)
    assert NO_ADDRESS_NOTE not in work_row


def test_done_page_after_relink_without_address_says_it_is_now_recorded(env):
    env.add_account("pessoal")
    _signed_in(env)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 200, resp.text
    assert "address is now recorded" in resp.text
    resp = _relink(env, "pessoal", "me@gmail.com")  # the second time it was already on record
    assert "address is now recorded" not in resp.text


@pytest.mark.parametrize("make_refusal", ["address-taken", "alias-taken", "different-mailbox"])
def test_refusals_after_google_exchange_point_to_permissions(env, make_refusal):
    if make_refusal == "address-taken":
        env.add_account("work", "box@example.test")
        env.ctx.book.record_linked("work", "gmail", "box@example.test", datetime.now(UTC))
        _signed_in(env)
        resp = _link(env, "pessoal", "box@example.test")
    elif make_refusal == "alias-taken":
        _signed_in(env)
        env.linker.result = LinkResult(credential_json=CRED, address="box@example.test")
        flow, state = _flow_and_state(_start_link(env, "pessoal"))
        env.add_account("pessoal", "other@example.test")
        resp = _oauth_callback(env, flow, state)
    else:
        env.add_account("pessoal", "me@gmail.com")
        env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
        _signed_in(env)
        resp = _relink(env, "pessoal", "other@gmail.com")
    assert resp.status_code == 400
    assert _visible(resp.text).count(GRANT_HINT) == 1
    assert f'href="{PERMISSIONS}"' in resp.text


def _raise(*a, **k):
    raise OSError("disk full")


def _failing_once(real):
    calls = []

    def wrapper(*a, **k):
        if not calls:
            calls.append(a)
            raise OSError("disk full")
        return real(*a, **k)

    return wrapper


def test_link_rolls_back_when_the_book_write_fails(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _signed_in(env)
    book_before = env.ctx.book.path.read_text() if env.ctx.book.path.exists() else None
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    resp = _link(env, "pessoal")
    assert resp.status_code == 500
    assert "Nothing was changed" in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    assert (env.ctx.book.path.read_text() if env.ctx.book.path.exists() else None) == book_before
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("admin link failed alias=pessoal step=book") for m in messages)
    assert "secret-refresh" not in "\n".join(messages)


def test_link_rolls_back_when_the_registry_update_fails(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _signed_in(env)
    monkeypatch.setattr(env.ctx.registry, "upsert", _failing_once(env.ctx.registry.upsert))
    resp = _link(env, "pessoal")
    assert resp.status_code == 500 and "Nothing was changed" in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    assert env.ctx.book.get("pessoal") is None
    assert any(r.getMessage().startswith("admin link failed alias=pessoal step=registry")
               for r in caplog.records)


def test_relink_rolls_back_credential_and_registry_when_the_book_write_fails(env, monkeypatch):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime(2026, 9, 1, tzinfo=UTC))
    path = _cred_path(env, "pessoal")
    path.write_text('{"refresh_token": "old"}')
    old = env.ctx.registry.get("pessoal")
    book_before = env.ctx.book.path.read_text()
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 500 and "Nothing was changed" in resp.text
    assert path.read_text() == '{"refresh_token": "old"}'
    restored = env.ctx.registry.get("pessoal")
    assert restored.provider_name == old.provider_name and restored.credential_path == old.credential_path
    assert env.ctx.book.path.read_text() == book_before


def test_relink_of_removed_alias_rolls_back_to_removed(env, monkeypatch):
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    env.ctx.book.record_removed("pessoal", datetime.now(UTC))
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 500
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    assert env.ctx.book.get("pessoal").removed_at


def test_remove_whose_record_fails_says_so(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_removed", _raise)
    resp = _post(env, "/admin/accounts/pessoal/remove", {"confirm": "pessoal"})
    assert resp.status_code == 500
    assert "removed, but its removal could not be recorded" in resp.text
    assert "http-equiv" not in resp.text
    assert not _cred_path(env, "pessoal").exists()
    assert "pessoal" not in env.ctx.registry.aliases()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("alias=pessoal" in r.getMessage() for r in errors)


# --- Task 8b fix round 1 ----------------------------------------------------------------------


def test_failed_rollback_is_not_reported_as_nothing_changed(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mailhub-mcp")
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    monkeypatch.setattr(accounts_module, "remove_credential", _raise)  # the undo of the new file fails
    resp = _link(env, "pessoal")
    assert resp.status_code == 500
    assert "Nothing was changed" not in resp.text
    assert "undo was incomplete" in resp.text and "server log" in resp.text
    assert _visible(resp.text).count(GRANT_HINT) == 1
    assert any(r.getMessage().startswith("admin link rollback failed alias=pessoal") for r in caplog.records)


def test_failed_registry_restore_on_relink_is_reported(env, monkeypatch):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    _signed_in(env)
    real_upsert = env.ctx.registry.upsert
    calls = []

    def upsert(*a, **k):  # the first upsert (the link) works, the restore fails
        calls.append(a)
        if len(calls) > 1:
            raise OSError("boom")
        return real_upsert(*a, **k)

    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    monkeypatch.setattr(env.ctx.registry, "upsert", upsert)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert resp.status_code == 500
    assert "undo was incomplete" in resp.text and "Nothing was changed" not in resp.text


def test_link_failure_page_carries_the_grant_hint(env, monkeypatch):
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    resp = _link(env, "pessoal")
    assert "Nothing was changed" in resp.text
    assert _visible(resp.text).count(GRANT_HINT) == 1


def test_relink_rollback_restores_the_credential_byte_for_byte(env, monkeypatch):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    old = b'{"refresh_token": "old"}\r\n\xff'  # CRLF and a byte that is not UTF-8
    _cred_path(env, "pessoal").write_bytes(old)
    _signed_in(env)
    monkeypatch.setattr(env.ctx.book, "record_linked", _raise)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert "Nothing was changed" in resp.text  # the rollback ran, not an early crash
    assert _cred_path(env, "pessoal").read_bytes() == old


def test_done_page_with_address_note_does_not_refresh(env):
    env.add_account("pessoal")
    _signed_in(env)
    resp = _relink(env, "pessoal", "me@gmail.com")
    assert "address is now recorded" in resp.text
    assert "http-equiv" not in resp.text


def test_remove_whose_record_fails_keeps_the_address_check(env, monkeypatch):
    env.add_account("pessoal", "me@gmail.com")
    env.ctx.book.record_linked("pessoal", "gmail", "me@gmail.com", datetime.now(UTC))
    _signed_in(env)
    with monkeypatch.context() as m:
        m.setattr(env.ctx.book, "record_removed", _raise)
        resp = _post(env, "/admin/accounts/pessoal/remove", {"confirm": "pessoal"})
    assert "The mailbox was removed, but its removal could not be recorded; see the server log." in resp.text
    assert "will not be checked" not in resp.text
    refused = _relink(env, "pessoal", "other@gmail.com")
    assert refused.status_code == 400
    assert "That is a different mailbox than pessoal (expected me@gmail.com)" in refused.text
    assert not _cred_path(env, "pessoal").exists()
