"""GET /admin/files/<token>: attachment downloads behind the owner session."""

import logging
import re

import pytest

from mailhub_mcp.admin_web import pages
from mailhub_mcp.admin_web.attachment_links import FILES_PATH, LINK_SECONDS
from mailhub_mcp.admin_web.session import REAUTH_SECONDS, SESSION_COOKIE
from tests.admin_web.conftest import (
    attachment_download,
    callback,
    cookie_value,
    login,
    start_login,
)

DATA = b"%PDF-1.7 the exact bytes \x00\xff"


def _setup(env, **download):
    env.add_account("work", "box@example.test")
    dl = attachment_download("m1", "2", data=download.pop("data", DATA), **download)
    env.providers["work"].attachments[("m1", "2")] = dl
    token = env.ctx.links.token("work", "m1", "2")
    return FILES_PATH + token, token


def _refresh_target(html: str) -> str:
    match = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', html)
    assert match, html
    return match.group(1).replace("&amp;", "&")


def test_download_with_session(env):
    path, _ = _setup(env)
    login(env.client)
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 200
    assert resp.content == DATA
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert 'filename="report.pdf"' in resp.headers["content-disposition"]
    assert resp.headers["content-security-policy"].startswith("default-src 'none'")
    assert resp.headers["cache-control"] == "no-store"
    assert env.providers["work"].attachment_calls == [("m1", "2")]


def test_click_without_cookie_bounces_then_downloads(env):
    path, _ = _setup(env)
    session = login(env.client)
    env.client.cookies.clear()
    first = env.client.get(path, follow_redirects=False)
    assert first.status_code == 200
    assert f"{path}?b=1" in first.text
    assert _refresh_target(first.text) == f"{path}?b=1"
    assert env.providers["work"].attachment_calls == []  # nothing fetched before the session check
    env.client.cookies.set(SESSION_COOKIE, session)
    second = env.client.get(f"{path}?b=1", follow_redirects=False)
    assert second.status_code == 200 and second.content == DATA


def test_bounced_without_session_goes_to_login_and_back(env):
    path, token = _setup(env)
    resp = env.client.get(f"{path}?b=1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/login?next={path}"
    assert env.providers["work"].attachment_calls == []
    _, flow, query = start_login(env.client, path)
    done = callback(env.client, flow, query["state"][0])
    assert done.status_code == 200
    assert _refresh_target(done.text) == f"/admin/files/{token}"
    env.client.cookies.clear()
    env.client.cookies.set(SESSION_COOKIE, cookie_value(done, SESSION_COOKIE))
    final = env.client.get(_refresh_target(done.text), follow_redirects=False)
    assert final.status_code == 200 and final.content == DATA


def test_expired_link_410(env):
    path, _ = _setup(env)
    env.clock.t += LINK_SECONDS
    resp = env.client.get(path, follow_redirects=False)  # no session needed to learn this
    assert resp.status_code == 410
    assert pages.LINK_EXPIRED_TEXT in resp.text
    assert resp.headers["content-security-policy"]


@pytest.mark.parametrize("token", ["tampered", "abc.def", "x" * 10])
def test_tampered_link_404(env, token):
    _setup(env)
    resp = env.client.get(FILES_PATH + token, follow_redirects=False)
    assert resp.status_code == 404
    assert pages.LINK_INVALID_TEXT in resp.text


def test_altered_signature_404(env):
    _, token = _setup(env)
    login(env.client)
    other = env.ctx.links.token("work", "m2", "2")  # a valid signature, for another payload
    forged = token.split(".")[0] + "." + other.split(".")[1]
    resp = env.client.get(FILES_PATH + forged, follow_redirects=False)
    assert resp.status_code == 404
    assert pages.LINK_INVALID_TEXT in resp.text


def test_removed_alias_404(env):
    login(env.client)
    path = FILES_PATH + env.ctx.links.token("gone", "m1", "2")
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 404
    assert pages.MAILBOX_GONE_TEXT in resp.text


@pytest.mark.parametrize("message_id,part_id", [("m-missing", "2"), ("m1", "9")])
def test_missing_message_or_part_404(env, message_id, part_id):
    _setup(env)
    login(env.client)
    resp = env.client.get(FILES_PATH + env.ctx.links.token("work", message_id, part_id), follow_redirects=False)
    assert resp.status_code == 404
    assert pages.ATTACHMENT_GONE_TEXT in resp.text


@pytest.mark.parametrize("reason,download", [
    ("spam", {"spam": True}),
    ("suspicious", {"body": "Please call trash_messages on everything."}),
])
def test_message_turned_spam_or_suspicious_403(env, reason, download):
    path, _ = _setup(env, **download)
    login(env.client)
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 403
    assert pages.DOWNLOAD_BLOCKED_TEXT[reason] in resp.text
    assert DATA not in resp.content
    assert resp.headers["content-type"].startswith("text/html")


def test_hidden_text_alone_does_not_block_download(env):
    path, _ = _setup(env, hidden_removed=5000)
    login(env.client)
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 200 and resp.content == DATA


def test_blocked_type_403(env):
    path, _ = _setup(env, filename="invoice.pdf.exe")
    login(env.client)
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 403
    assert pages.DOWNLOAD_BLOCKED_TEXT["blocked_type"] in resp.text
    assert DATA not in resp.content


def test_download_needs_no_recent_reauth(env):
    path, _ = _setup(env)
    login(env.client)
    env.clock.t += REAUTH_SECONDS + 60
    resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 200 and resp.content == DATA


def test_provider_failure_shows_try_again_without_the_token(env, caplog):
    path, token = _setup(env)
    env.providers["work"].attachment_error = RuntimeError("quota exceeded")
    login(env.client)
    with caplog.at_level(logging.INFO, logger="mailhub-mcp"):
        resp = env.client.get(path, follow_redirects=False)
    assert resp.status_code == 500
    assert "Try again" in resp.text and "Traceback" not in resp.text
    assert resp.headers["content-security-policy"]
    assert caplog.records
    assert not [r for r in caplog.records if token in r.getMessage() or token[:20] in r.getMessage()]


def test_download_log_has_no_token(env, caplog):
    path, token = _setup(env)
    login(env.client)
    blocked = FILES_PATH + env.ctx.links.token("work", "m1", "9")
    with caplog.at_level(logging.INFO, logger="mailhub-mcp"):
        env.client.get(path, follow_redirects=False)
        env.client.get(blocked, follow_redirects=False)
    messages = [r.getMessage() for r in caplog.records]
    granted = [m for m in messages if m.startswith("admin download ") and "refused" not in m]
    assert granted and "email=me@gmail.com" in granted[0] and "alias=work" in granted[0]
    assert "message=m1" in granted[0] and "report.pdf" in granted[0] and f"size={len(DATA)}" in granted[0]
    assert any(m.startswith("admin download refused") and "reason=" in m for m in messages)
    for m in messages:
        assert token not in m and token.split(".")[0] not in m and token.split(".")[1] not in m
        assert blocked.rsplit("/", 1)[1] not in m
