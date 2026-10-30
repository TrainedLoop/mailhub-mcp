import json

import pytest

from mailhub_mcp import admin
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.core.preferences import PreferenceStore, set_preferences

REQUEST = "stop the promo mails \x1b[31mRED"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("PUBLIC_BASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAILS", "JWT_SIGNING_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "changes.jsonl"))
    store = PreferenceStore(tmp_path / "preferences.json")
    journal = Journal(tmp_path / "changes.jsonl")
    res = set_preferences(store, journal, "global", add={"ignored_keywords": ["promo", "sale"]},
                          user_request=REQUEST, valid_scopes=[])
    return store, journal, res["pending"]


def run(capsys, *argv):
    code = admin.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_list_show_approve(env, capsys):
    store, journal, pending = env
    pid = pending[0]["proposal_id"]
    code, out, _ = run(capsys, "preferences", "list")
    assert code == 0 and pid in out and "ignored_keywords" in out
    code, out, _ = run(capsys, "preferences", "show", pid)
    assert code == 0
    for text in (pid, "global", "ignored_keywords", "promo", "stop the promo mails", "Created", "Expires"):
        assert text in out
    assert "\x1b" not in out
    code, out, _ = run(capsys, "preferences", "approve", pid)
    assert code == 0 and pid in out and "\x1b" not in out
    assert "promo" in store.load()["global"]["ignored_keywords"]
    entries = journal.entries()
    assert entries[0]["tool"] == "approve_preference"
    assert run(capsys, "preferences", "list")[1].strip().startswith("No pending")


def test_reject(env, capsys):
    store, journal, pending = env
    pid = pending[0]["proposal_id"]
    code, out, _ = run(capsys, "preferences", "reject", pid)
    assert code == 0 and "rejected" in out.lower()
    assert store.load()["proposals"] == [] and store.load()["global"]["ignored_keywords"] == []
    assert not journal.entries()


def test_errors_exit_2(env, capsys):
    code, _, err = run(capsys, "preferences", "approve", "p-nope")
    assert code == 2 and "Unknown proposal" in err
    assert run(capsys, "preferences", "show", "p-nope")[0] == 2
    assert run(capsys, "preferences", "reject", "p-nope")[0] == 2


def test_usage_error_exit_2(env, capsys):
    assert run(capsys, "preferences")[0] == 2
    assert run(capsys, "bogus")[0] == 2


def test_empty_state_lists_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "x" / "changes.jsonl"))
    code, out, _ = run(capsys, "preferences", "list")
    assert code == 0 and "No pending" in out
