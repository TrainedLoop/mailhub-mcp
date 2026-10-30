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


def test_show_unknown_says_unknown_or_expired(env, capsys):
    code, _, err = run(capsys, "preferences", "show", "p-nope")
    assert code == 2 and "unknown or expired proposal" in err.lower()


def test_show_cannot_forge_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "changes.jsonl"))
    store = PreferenceStore(tmp_path / "preferences.json")
    res = set_preferences(store, Journal(tmp_path / "changes.jsonl"), "global",
                          add={"ignored_keywords": ["promo"]},
                          user_request="ok\nExpires:      never ‮evil", valid_scopes=[])
    code, out, _ = run(capsys, "preferences", "show", res["pending"][0]["proposal_id"])
    assert code == 0 and "‮" not in out
    assert [l for l in out.splitlines() if l.startswith("Expires:")] == [l for l in out.splitlines() if l.startswith("Expires:")][:1]
    assert len([l for l in out.splitlines() if l.startswith("Expires:")]) == 1


def test_oserror_exits_2_without_traceback(env, capsys, monkeypatch):
    def boom(self):
        raise PermissionError("denied")
    monkeypatch.setattr(PreferenceStore, "load", boom)
    code, _, err = run(capsys, "preferences", "list")
    assert code == 2 and "denied" in err and "Traceback" not in err


def test_approve_undo_proposal(tmp_path, capsys, monkeypatch):
    from mailhub_mcp.core.organize import undo
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "changes.jsonl"))
    store = PreferenceStore(tmp_path / "preferences.json")
    journal = Journal(tmp_path / "changes.jsonl")
    cid = set_preferences(store, journal, "global", add={"important_keywords": ["rent"]},
                          user_request="rent matters", valid_scopes=[])["change_id"]
    pid = undo(None, journal, "global", cid, store=store)["proposed"][0]["proposal_id"]
    code, out, _ = run(capsys, "preferences", "show", pid)
    assert code == 0 and f"undo of {cid}: rent matters" in out
    code, out, _ = run(capsys, "preferences", "approve", pid)
    assert code == 0 and "remove important_keywords" in out
    assert store.load()["global"]["important_keywords"] == []
    assert journal.entries()[0]["undo_of"] == cid


def test_show_and_reject_undo_proposal(tmp_path, capsys, monkeypatch):
    from mailhub_mcp.core.organize import undo
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "changes.jsonl"))
    store = PreferenceStore(tmp_path / "preferences.json")
    journal = Journal(tmp_path / "changes.jsonl")
    cid = set_preferences(store, journal, "global", add={"important_keywords": ["rent"]},
                          user_request="rent matters", valid_scopes=[])["change_id"]
    pid = undo(None, journal, "global", cid, store=store)["proposed"][0]["proposal_id"]
    code, out, _ = run(capsys, "preferences", "show", pid)
    assert code == 0 and f"Undo of:      {cid}" in out
    assert "Undo of" not in run(capsys, "preferences", "list")[1]
    code, out, _ = run(capsys, "preferences", "reject", pid)
    assert code == 0 and f"The undo of {cid} will not be proposed again" in out
    assert store.load()["rejected_undos"] == [cid]


def test_show_plain_proposal_has_no_undo_line(env, capsys):
    _, _, pending = env
    assert "Undo of" not in run(capsys, "preferences", "show", pending[0]["proposal_id"])[1]


# --- accounts ---

class _Prov:
    def __init__(self, address=None, error=None):
        self._address, self._error = address, error

    def address(self):
        if self._error:
            raise self._error
        return self._address


def _accounts_env(tmp_path, monkeypatch, aliases):
    gmail = tmp_path / "accounts" / "gmail"
    gmail.mkdir(parents=True)
    for alias in aliases:
        (gmail / f"{alias}.json").write_text("{}")
    monkeypatch.setenv("ACCOUNTS_DIR", str(tmp_path / "accounts"))


def _run_accounts(capsys, providers):
    code = admin.main(["accounts"], providers=providers)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_accounts_all_ok(tmp_path, monkeypatch, capsys):
    _accounts_env(tmp_path, monkeypatch, ["alice", "bob"])
    by = {"alice": _Prov("a@x.com"), "bob": _Prov("b@x.com")}
    code, out, _ = _run_accounts(capsys, {"gmail": lambda alias, path: by[alias]})
    assert code == 0
    assert out.splitlines() == ["alice  gmail  ok  a@x.com", "bob  gmail  ok  b@x.com"]


def test_accounts_failure_exit_1_and_message_cleaned(tmp_path, monkeypatch, capsys):
    _accounts_env(tmp_path, monkeypatch, ["alice", "bob"])
    by = {"alice": _Prov("a@x.com"), "bob": _Prov(error=RuntimeError("token expired\n\x1b[31mRED"))}
    code, out, _ = _run_accounts(capsys, {"gmail": lambda alias, path: by[alias]})
    assert code == 1
    assert "alice  gmail  ok  a@x.com" in out
    assert "bob  gmail  ERROR  token expired [31mRED" in out and "\x1b" not in out


def test_accounts_factory_failure_is_an_error_row(tmp_path, monkeypatch, capsys):
    _accounts_env(tmp_path, monkeypatch, ["alice"])

    def boom(alias, path):
        raise ValueError("bad credential file")

    code, out, _ = _run_accounts(capsys, {"gmail": boom})
    assert code == 1 and "alice  gmail  ERROR  bad credential file" in out


def test_accounts_config_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACCOUNTS_DIR", str(tmp_path / "missing"))
    assert _run_accounts(capsys, {"gmail": lambda a, p: _Prov("x")})[0] == 2
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("ACCOUNTS_DIR", str(tmp_path / "empty"))
    assert _run_accounts(capsys, {"gmail": lambda a, p: _Prov("x")})[0] == 2
    _accounts_env(tmp_path, monkeypatch, ["alice"])
    other = tmp_path / "accounts" / "other"
    other.mkdir()
    (other / "alice.json").write_text("{}")
    code, _, err = _run_accounts(capsys, {"gmail": lambda a, p: _Prov("x"), "other": lambda a, p: _Prov("y")})
    assert code == 2 and "alice" in err


def test_accounts_unreadable_dir_exit_2(tmp_path, monkeypatch, capsys):
    _accounts_env(tmp_path, monkeypatch, ["alice"])

    def deny(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.is_dir", deny)
    code, _, err = _run_accounts(capsys, {"gmail": lambda a, p: _Prov("x")})
    assert code == 2 and "cannot read the accounts directory" in err and "Traceback" not in err


def test_accounts_needs_no_oauth_env(tmp_path, monkeypatch, capsys):
    for k in ("PUBLIC_BASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAILS", "JWT_SIGNING_KEY"):
        monkeypatch.delenv(k, raising=False)
    _accounts_env(tmp_path, monkeypatch, ["alice"])
    assert _run_accounts(capsys, {"gmail": lambda a, p: _Prov("a@x.com")})[0] == 0
