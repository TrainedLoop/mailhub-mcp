import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.auth.exceptions import RefreshError

from mailhub_mcp import server
from mailhub_mcp.auth import AllowlistGoogleProvider
from mailhub_mcp.core.accounts import AccountRegistry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.providers.gmail.provider import GmailProvider
from tests.fakes.fake_gmail import FakeGmail, http_error

TOKEN = {"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"}


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    a, b = FakeGmail("a@gmail.com"), FakeGmail("b@work.com")
    a.add_message("m1", labels=["INBOX", "UNREAD"], headers={"Subject": "Hi", "From": "x <x@y.com>"}, internal_date=5)
    a.add_message("m2", labels=["INBOX"], internal_date=3)
    b.add_message("w1", labels=["INBOX"], internal_date=9)
    by_alias = {"alice-personal": a, "bob-work": b}
    gmail_dir = tmp_path / "accounts" / "gmail"
    gmail_dir.mkdir(parents=True)
    for alias in by_alias:
        (gmail_dir / f"{alias}.json").write_text(json.dumps(TOKEN))
    providers = {
        "gmail": lambda alias, path: GmailProvider(alias, path, build_service=lambda c: by_alias[alias])
    }
    monkeypatch.setattr(server, "registry", AccountRegistry(tmp_path / "accounts", providers))
    monkeypatch.setattr(server, "journal", Journal(tmp_path / "j.jsonl"))
    return SimpleNamespace(a=a, b=b)


def call(name, args=None):
    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool(name, args or {})
            return r.structured_content
    return asyncio.run(go())


def has_write(fake):
    return any(c[0] == "messages.batchModify" for c in fake.calls)


def test_tool_names():
    async def go():
        async with Client(server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    assert asyncio.run(go()) == {
        "list_accounts", "search_emails", "read_email", "read_emails", "read_thread", "inbox_overview",
        "list_drafts", "list_labels", "create_label", "modify_emails", "modify_by_query", "undo_change",
        "create_draft", "list_changes", "undo_since",
    }


def test_annotations():
    async def go():
        async with Client(server.mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    tools = asyncio.run(go())
    assert tools["search_emails"].annotations.read_only_hint is True
    assert tools["read_email"].description.endswith(server.UNTRUSTED_NOTE)
    assert tools["modify_emails"].annotations.read_only_hint is False
    assert "exclude emails already changed" in tools["modify_by_query"].description
    assert "in:inbox" in tools["modify_by_query"].description
    assert tools["modify_emails"].annotations.destructive_hint is False


def test_list_accounts(fakes):
    fakes.b.fail = RefreshError("bad")
    out = call("list_accounts")
    by = {a["alias"]: a for a in out["accounts"]}
    assert by["alice-personal"] == {
        "alias": "alice-personal",
        "provider": "gmail",
        "query_hint": GmailProvider.query_hint,
        "email": "a@gmail.com",
        "status": "ok",
    }
    assert set(by["bob-work"]) == {"alias", "provider", "query_hint", "email", "status"}
    assert by["bob-work"]["email"] is None


def test_search_aggregates_with_account_and_errors(fakes):
    fakes.b.fail = RefreshError("bad")
    out = call("search_emails", {"query": "is:unread"})
    assert {r["account"] for r in out["results"]} == {"alice-personal"} and "bob-work" in out["errors"]


def test_search_subset_and_unknown_account(fakes):
    out = call("search_emails", {"query": "x", "accounts": ["bob-work"]})
    assert {r["account"] for r in out["results"]} == {"bob-work"}
    with pytest.raises(ToolError, match="alice-personal"):
        call("search_emails", {"query": "x", "accounts": ["zzz"]})


def test_read_email_unknown_account_lists_valid(fakes):
    with pytest.raises(ToolError, match="alice-personal"):
        call("read_email", {"account": "zzz", "message_id": "m1"})


def test_read_email_ok_and_gmail_error_prefixed(fakes):
    out = call("read_email", {"account": "alice-personal", "message_id": "m1"})
    assert out["id"] == "m1" and out["account"] == "alice-personal"
    with pytest.raises(ToolError, match="bob-work"):
        call("read_email", {"account": "bob-work", "message_id": "nope"})


def test_read_emails_caps_at_25(fakes):
    with pytest.raises(ToolError, match="25"):
        call("read_emails", {"account": "alice-personal", "message_ids": [f"m{i}" for i in range(26)]})
    assert fakes.a.calls == []


def test_read_emails_ok(fakes):
    out = call("read_emails", {"account": "alice-personal", "message_ids": ["m1", "zz"]})
    assert [m["id"] for m in out["messages"]] == ["m1"] and "zz" in out["errors"]
    assert all(m["account"] == "alice-personal" for m in out["messages"])


def test_read_thread(fakes):
    msgs = call("read_thread", {"account": "alice-personal", "thread_id": "m1"})["messages"]
    assert len(msgs) == 1 and all(m["account"] == "alice-personal" for m in msgs)


def test_inbox_overview(fakes):
    out = call("inbox_overview")
    assert {r["account"] for r in out["results"]} == {"alice-personal", "bob-work"}
    assert next(r for r in out["results"] if r["account"] == "alice-personal")["total"] == 2


def test_list_labels_and_create_label(fakes):
    assert any(l["name"] == "INBOX" for l in call("list_labels", {"account": "alice-personal"})["labels"])
    assert call("create_label", {"account": "alice-personal", "name": "New"})["name"] == "New"


def test_modify_emails_and_blocked_label(fakes):
    out = call("modify_emails", {"account": "alice-personal", "message_ids": ["m1"], "archive": True})
    assert out["modified"] == 1 and "INBOX" not in fakes.a.messages["m1"]["labelIds"]
    with pytest.raises(ToolError, match="cannot be changed"):
        call("modify_emails", {"account": "alice-personal", "message_ids": ["m2"], "add_labels": ["TRASH"]})


def test_modify_by_query_defaults_to_preview(fakes):
    out = call("modify_by_query", {"account": "alice-personal", "query": "in:inbox", "archive": True})
    assert "matched" in out and out["matched"] == 2
    assert not has_write(fakes.a)


def test_modify_by_query_confirm_applies(fakes):
    out = call("modify_by_query", {"account": "alice-personal", "query": "in:inbox", "archive": True, "confirm": True})
    assert out["modified"] == 2 and has_write(fakes.a)


def test_undo_change_via_tool_roundtrip(fakes):
    ch = call("modify_emails", {"account": "alice-personal", "message_ids": ["m1"], "archive": True})["change_id"]
    out = call("undo_change", {"change_id": ch})
    assert out["undone"] == ch and "INBOX" in fakes.a.messages["m1"]["labelIds"]


def test_undo_unknown_change_id(fakes):
    with pytest.raises(ToolError, match="nope"):
        call("undo_change", {"change_id": "nope"})


def test_create_draft_requires_account(fakes):
    with pytest.raises(ToolError):
        call("create_draft", {"to": "x@y.com", "body": "hi"})
    out = call("create_draft", {"account": "alice-personal", "to": "x@y.com", "body": "hi", "subject": "s"})
    assert out["draft_id"]


def test_list_drafts(fakes):
    out = call("list_drafts")
    assert out["errors"] == {} and out["results"] == []


def test_message_output_uses_from_key(fakes):
    out = call("search_emails", {"query": "x", "accounts": ["alice-personal"]})
    item = out["results"][0]
    assert "from" in item and "from_" not in item
    assert "from" in call("read_email", {"account": "alice-personal", "message_id": "m1"})


def test_list_accounts_has_provider_and_hint(fakes):
    by = {a["alias"]: a for a in call("list_accounts")["accounts"]}
    assert by["alice-personal"]["provider"] == "gmail"
    assert by["alice-personal"]["query_hint"] == GmailProvider.query_hint


def test_inbox_overview_default_query_and_failed(fakes):
    fakes.a.get_errors["m2"] = http_error(404, "gone")
    out = call("inbox_overview", {"accounts": ["alice-personal"]})
    assert any(n == "messages.list" and kw.get("q") == "in:inbox" for n, kw in fakes.a.calls)
    assert out["results"][0]["failed"] == 1 and out["results"][0]["total"] == 1


def test_auth_expired_status_has_hint(fakes):
    fakes.b.fail = RefreshError("bad")
    by = {a["alias"]: a for a in call("list_accounts")["accounts"]}
    assert "run uv run --extra authorize mailhub-authorize gmail <client_secret.json> bob-work again" in by["bob-work"]["status"]


def test_list_labels_has_untrusted_note():
    async def go():
        async with Client(server.mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    assert asyncio.run(go())["list_labels"].description.endswith(server.UNTRUSTED_NOTE)


class Claims:
    def __init__(self, claims):
        self.claims = claims


def allowed(claims):
    async def go():
        prov = AllowlistGoogleProvider.__new__(AllowlistGoogleProvider)
        prov.allowed_emails = frozenset({"me@gmail.com"})
        from fastmcp.server.auth.providers.google import GoogleProvider

        async def fake_super(self, token):
            return Claims(claims) if claims is not None else None

        orig = GoogleProvider.load_access_token
        GoogleProvider.load_access_token = fake_super
        try:
            return await AllowlistGoogleProvider.load_access_token(prov, "t") is not None
        finally:
            GoogleProvider.load_access_token = orig
    return asyncio.run(go())


def test_allowlist():
    assert allowed({"email": "me@gmail.com", "email_verified": True})
    assert allowed({"email": "ME@gmail.com", "email_verified": "true"})
    assert not allowed({"email": "me@gmail.com", "email_verified": False})
    assert not allowed({"email": "other@gmail.com", "email_verified": True})
    assert not allowed({})


def test_search_reports_unreadable_messages_but_returns_the_rest(fakes):
    fakes.a.get_errors["m2"] = http_error(403, "forbidden")
    out = call("search_emails", {"query": "in:inbox", "accounts": ["alice-personal"]})
    assert out["errors"]["alice-personal"] == "1 message could not be read"
    assert [r["id"] for r in out["results"]] == ["m1"]


def test_search_without_unreadable_messages_has_no_errors(fakes):
    out = call("search_emails", {"query": "in:inbox", "accounts": ["alice-personal"]})
    assert out["errors"] == {}


def test_read_emails_errors_are_readable_not_raw(fakes, monkeypatch):
    monkeypatch.setattr("mailhub_mcp.providers.gmail.api._sleep", lambda s: None)
    fakes.a.get_errors["m1"] = http_error(503, "backend exploded")
    out = call("read_emails", {"account": "alice-personal", "message_ids": ["m1", "zz"]})
    assert out["errors"] == {"m1": "temporary failure, retry", "zz": "message not found"}


def test_search_pluralizes_unreadable_count(fakes):
    for i in ("m1", "m2"):
        fakes.a.get_errors[i] = http_error(403, "forbidden")
    out = call("search_emails", {"query": "in:inbox", "accounts": ["alice-personal"]})
    assert out["errors"]["alice-personal"] == "2 messages could not be read"


def test_search_ignores_messages_that_vanish_between_list_and_fetch(fakes):
    fakes.a.get_errors["m2"] = http_error(404, "gone")
    out = call("search_emails", {"query": "in:inbox", "accounts": ["alice-personal"]})
    assert out["errors"] == {} and [r["id"] for r in out["results"]] == ["m1"]


def test_read_emails_classifies_non_retryable_failures(fakes):
    fakes.a.get_errors["m1"] = http_error(403, "forbidden")
    out = call("read_emails", {"account": "alice-personal", "message_ids": ["m1", "zz"]})
    assert out["errors"] == {"m1": "could not be read", "zz": "message not found"}


def test_settings_port_default_and_override(monkeypatch):
    from mailhub_mcp.config import Settings

    monkeypatch.delenv("PORT", raising=False)
    assert Settings.from_env().port == 8742
    monkeypatch.setenv("PORT", "9001")
    assert Settings.from_env().port == 9001


def _change(fakes, alias="alice-personal", ids=("m1",), **kw):
    return call("modify_emails", {"account": alias, "message_ids": list(ids), **kw})["change_id"]


def test_list_changes_order_limit_total_and_label_names(fakes):
    c1 = _change(fakes, archive=True)
    c2 = _change(fakes, "bob-work", ids=("w1",), archive=True)
    c3 = _change(fakes, ids=("m2",), archive=True)
    out = call("list_changes")
    assert [c["change_id"] for c in out["changes"]] == [c3, c2, c1] and out["total"] == 3
    top = out["changes"][0]
    assert top["account"] == "alice-personal" and top["messages"] == 1 and top["undone"] is False
    assert "claude/organized" in top["added"] and top["removed"] == ["INBOX"]
    out = call("list_changes", {"limit": 1})
    assert [c["change_id"] for c in out["changes"]] == [c3] and out["total"] == 3
    out = call("list_changes", {"limit": 0})
    assert len(out["changes"]) == 1
    out = call("list_changes", {"accounts": ["bob-work"]})
    assert [c["change_id"] for c in out["changes"]] == [c2] and out["total"] == 1


def test_list_changes_since_undone_flag_and_label_failure_fallback(fakes):
    c1 = _change(fakes, archive=True)
    call("undo_change", {"change_id": c1})
    out = call("list_changes")
    by = {c["change_id"]: c for c in out["changes"]}
    assert by[c1]["undone"] is True
    assert [c["undo_of"] for c in out["changes"]][0] == c1
    assert call("list_changes", {"since": "2999-01-01T00:00"})["changes"] == []
    assert call("list_changes", {"since": "2000-01-01"})["total"] == 2
    fakes.a.fail = RefreshError("bad")
    out = call("list_changes")  # label lookup fails: ids are shown, call still succeeds
    assert out["total"] == 2 and any(c["removed"] == ["INBOX"] for c in out["changes"])


def test_list_changes_errors(fakes):
    with pytest.raises(ToolError, match="invalid since: expected ISO 8601 date/time, e.g. 2026-11-01T08:00"):
        call("list_changes", {"since": "yesterday"})
    with pytest.raises(ToolError, match="nope"):
        call("list_changes", {"accounts": ["nope"]})


def test_undo_since_preview_then_confirm(fakes):
    _change(fakes, archive=True)
    _change(fakes, "bob-work", ids=("w1",), archive=True)
    fakes.a.calls.clear(); fakes.b.calls.clear()
    out = call("undo_since", {"since": "2000-01-01"})
    assert out["total"] == 2 and not has_write(fakes.a) and not has_write(fakes.b)
    assert "claude/organized" in out["changes"][0]["added"]
    out = call("undo_since", {"since": "2000-01-01T00:00:00+00:00", "accounts": ["alice-personal"], "confirm": True})
    assert len(out["undone"]) == 1 and out["failed"] == {} and out["remaining"] is False
    assert "INBOX" in fakes.a.messages["m1"]["labelIds"] and "INBOX" not in fakes.b.messages["w1"]["labelIds"]


def test_undo_since_errors_and_annotations(fakes):
    with pytest.raises(ToolError, match="invalid since"):
        call("undo_since", {"since": "soon"})
    with pytest.raises(ToolError, match="nope"):
        call("undo_since", {"since": "2000-01-01", "accounts": ["nope"]})

    async def go():
        async with Client(server.mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    tools = asyncio.run(go())
    assert tools["list_changes"].annotations.read_only_hint is True
    assert tools["undo_since"].annotations.read_only_hint is False
    assert tools["undo_since"].annotations.destructive_hint is False
    assert "change_id" in tools["list_changes"].description
    assert "remaining" in tools["undo_since"].description


def test_undo_since_failure_reported_with_account_prefix(fakes):
    c1 = _change(fakes, archive=True)
    fakes.a.fail = RefreshError("bad")
    out = call("undo_since", {"since": "2000-01-01", "confirm": True})
    assert out["undone"] == [] and out["failed"][c1].startswith("alice-personal: ")
