from datetime import datetime, timedelta, timezone

import pytest

from mailhub_mcp.core.history import summarize_entry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.core.organize import MAX_UNDO_SINCE, modify, undo, undo_since
from mailhub_mcp.providers.gmail.provider import GmailProvider
from tests.fakes.fake_gmail import FakeGmail, http_error

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def test_summarize_entry_names_fallback_counts_and_flags():
    entry = {
        "change_id": "c1", "timestamp": "2026-01-01T00:00:00+00:00", "account": "a",
        "tool": "modify_by_query", "added": ["L1", "STARRED"], "removed": ["INBOX", "L9"],
        "prior": {"m1": [], "m2": ["INBOX"]}, "query": "in:inbox", "undo_of": None,
    }
    out = summarize_entry(entry, False, {"L1": "claude/organized", "INBOX": "INBOX"})
    assert out == {
        "change_id": "c1", "timestamp": "2026-01-01T00:00:00+00:00", "account": "a",
        "tool": "modify_by_query", "query": "in:inbox", "messages": 2,
        "added": ["claude/organized", "STARRED"], "removed": ["INBOX", "L9"],
        "undone": False, "undo_of": None,
    }
    undo_entry = {**entry, "undo_of": "c0", "query": None}
    out = summarize_entry(undo_entry, True, {})
    assert out["undone"] is True and out["undo_of"] == "c0" and out["query"] is None


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    monkeypatch.setattr(
        "mailhub_mcp.providers.gmail.provider.Credentials.from_authorized_user_file",
        lambda *a, **k: object(),
    )


@pytest.fixture
def fake():
    f = FakeGmail()
    f.add_message("m1", labels=["INBOX", "UNREAD", "STARRED"])
    f.add_message("m2", labels=["INBOX", "UNREAD"])
    return f


@pytest.fixture
def provider(fake, tmp_path):
    return GmailProvider("a", tmp_path / "a.json", build_service=lambda c: fake)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "c.jsonl")


def test_preview_writes_nothing(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    fake.calls.clear()
    before = list(journal.entries())
    out = undo_since(lambda a: provider, journal, EPOCH, None, False)
    assert out["total"] == 1 and out["remaining"] is False
    assert out["changes"][0]["messages"] == 1 and out["changes"][0]["undone"] is False
    assert not any(c[0] == "messages.batchModify" for c in fake.calls)
    assert journal.entries() == before


def test_confirm_undoes_newest_first_overlapping_restore_exact(fake, provider, journal):
    original = sorted(fake.messages["m1"]["labelIds"])
    c1 = modify(provider, journal, "a", ["m1"], [], [], True, None, None)["change_id"]
    c2 = modify(provider, journal, "a", ["m1"], [], [], None, True, False)["change_id"]
    out = undo_since(lambda a: provider, journal, EPOCH, None, True)
    assert [u["change_id"] for u in out["undone"]] == [c2, c1]
    assert out["failed"] == {} and out["remaining"] is False
    assert all(u["undo_change_id"] and u["modified"] == 1 and u["skipped"] == [] for u in out["undone"])
    assert sorted(fake.messages["m1"]["labelIds"]) == original


def test_excludes_undone_and_undo_entries_and_second_run_is_empty(fake, provider, journal):
    c1 = modify(provider, journal, "a", ["m1"], [], [], True, None, None)["change_id"]
    c2 = modify(provider, journal, "a", ["m2"], [], [], True, None, None)["change_id"]
    undo(provider, journal, "a", c1)
    out = undo_since(lambda a: provider, journal, EPOCH, None, False)
    assert [c["change_id"] for c in out["changes"]] == [c2]
    undo_since(lambda a: provider, journal, EPOCH, None, True)
    again = undo_since(lambda a: provider, journal, EPOCH, None, False)
    assert again == {"changes": [], "total": 0, "remaining": False}


def test_since_filters_old_entries(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert undo_since(lambda a: provider, journal, future, None, False)["total"] == 0


def test_accounts_filter(fake, provider, journal):
    ca = modify(provider, journal, "a", ["m1"], [], [], True, None, None)["change_id"]
    journal.record("b", "modify_emails", ["X"], [], {"z": []})
    out = undo_since(lambda a: provider, journal, EPOCH, ["a"], False)
    assert [c["change_id"] for c in out["changes"]] == [ca]
    out = undo_since(lambda a: provider, journal, EPOCH, ["b"], False)
    assert out["total"] == 1 and out["changes"][0]["account"] == "b"


def test_cap_of_50_with_remaining(fake, provider, journal):
    for _ in range(MAX_UNDO_SINCE + 3):
        journal.record("a", "modify_emails", ["X"], [], {"m1": []})
    preview = undo_since(lambda a: provider, journal, EPOCH, None, False)
    assert len(preview["changes"]) == MAX_UNDO_SINCE and preview["total"] == 53 and preview["remaining"] is True
    out = undo_since(lambda a: provider, journal, EPOCH, None, True)
    assert len(out["undone"]) == MAX_UNDO_SINCE and out["remaining"] is True
    out = undo_since(lambda a: provider, journal, EPOCH, None, True)
    assert len(out["undone"]) == 3 and out["remaining"] is False


def test_one_failure_is_reported_and_others_succeed(fake, provider, journal):
    c1 = modify(provider, journal, "a", ["m1"], [], [], True, None, None)["change_id"]
    c2 = modify(provider, journal, "a", ["m2"], [], [], True, None, None)["change_id"]
    fake.get_errors["m2"] = http_error(503, "Unavailable")
    out = undo_since(lambda a: provider, journal, EPOCH, None, True)
    assert [u["change_id"] for u in out["undone"]] == [c1]
    assert list(out["failed"]) == [c2] and "m2" in out["failed"][c2]
    assert "INBOX" in fake.messages["m1"]["labelIds"] and "INBOX" not in fake.messages["m2"]["labelIds"]
    del fake.get_errors["m2"]
    out = undo_since(lambda a: provider, journal, EPOCH, None, True)
    assert [u["change_id"] for u in out["undone"]] == [c2] and out["failed"] == {}


def test_provider_lookup_failure_is_reported_per_change(fake, provider, journal):
    cb = journal.record("b", "modify_emails", ["X"], [], {"z": []})
    ca = modify(provider, journal, "a", ["m1"], [], [], True, None, None)["change_id"]

    def provider_for(alias):
        if alias == "b":
            raise KeyError("b")
        return provider

    out = undo_since(provider_for, journal, EPOCH, None, True)
    assert [u["change_id"] for u in out["undone"]] == [ca] and cb in out["failed"]


def test_preview_resolves_label_names_when_given_and_survives_lookup_errors(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    out = undo_since(lambda a: provider, journal, EPOCH, None, False,
                     label_names_for=lambda a: {l.id: l.name for l in provider.list_labels()})
    assert "claude/organized" in out["changes"][0]["added"] and "INBOX" in out["changes"][0]["removed"]

    def boom(a):
        raise RuntimeError("x")

    out = undo_since(lambda a: provider, journal, EPOCH, None, False, label_names_for=boom)
    assert out["total"] == 1
