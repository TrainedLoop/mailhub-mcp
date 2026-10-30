import json
import multiprocessing
import threading
from datetime import datetime, timedelta, timezone

import pytest

from mailhub_mcp.core import preferences as prefs
from mailhub_mcp.core.errors import MailhubError, OrganizeError
from mailhub_mcp.core.history import summarize_entry
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.core.organize import undo, undo_since
from mailhub_mcp.core.preferences import (
    FIELDS, RAISING, PreferenceError, PreferenceStore, approve, list_proposals, normalize_keyword,
    normalize_sender, reject, set_preferences, view,
)

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SCOPES = ["a", "b"]


@pytest.fixture
def store(tmp_path):
    return PreferenceStore(tmp_path / "preferences.json")


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "c.jsonl")


def put(store, journal, scope="global", add=None, remove=None, triage=None, req="because", now=T0):
    return set_preferences(store, journal, scope, add, remove, triage, req, SCOPES, now)


def test_error_type():
    assert issubclass(PreferenceError, MailhubError) and issubclass(PreferenceError, ValueError)


def test_fields_and_raising_table():
    assert FIELDS[-1] == "last_triage_at" and len(FIELDS) == 5
    assert RAISING == {
        ("important_senders", "add"), ("important_keywords", "add"), ("ignored_senders", "remove"),
        ("ignored_keywords", "remove"), ("last_triage_at", "set"),
    }


@pytest.mark.parametrize("raw,out", [(" Bob@Example.COM ", "bob@example.com"), ("@Example.com", "@example.com")])
def test_sender_normalized(raw, out):
    assert normalize_sender(raw) == out


@pytest.mark.parametrize("bad", ["", "bob", "bob@", "@", "bob@x", "a b@x.com", "bob@@x.com", "x@" + "a" * 250 + ".com", 5, "a\n@b.com"])
def test_sender_invalid(bad):
    with pytest.raises(PreferenceError) as e:
        normalize_sender(bad)
    assert repr(bad) in str(e.value)


def test_keyword_rules():
    assert normalize_keyword("  Invoice due ") == "Invoice due"
    assert normalize_keyword("x" * 40)
    for bad in ["", "   ", "x" * 41, "a\nb", "a\x00b", "a​b", None]:
        with pytest.raises(PreferenceError):
            normalize_keyword(bad)


@pytest.mark.parametrize("kwargs", [
    {"scope": "zzz", "add": {"important_keywords": ["x"]}},
    {"add": {"nope": ["x"]}},
    {"add": {"last_triage_at": ["x"]}},
    {"add": {"important_keywords": "x"}},
    {"add": {"important_senders": ["bad"]}},
    {"add": {"important_keywords": [""]}},
    {"triage": "yesterday"},
    {"triage": "2027-01-01T00:00:00Z"},
    {},
    {"add": {"important_keywords": ["X"]}, "remove": {"important_keywords": ["x"]}},
    {"add": {"important_keywords": ["x"]}, "req": ""},
    {"add": {"important_keywords": ["x"]}, "req": "  "},
    {"add": {"important_keywords": ["x"]}, "req": "r" * 501},
])
def test_validation_refused_with_no_write(store, journal, kwargs):
    with pytest.raises(PreferenceError):
        put(store, journal, **kwargs)
    assert not store.path.exists() and not journal.path.exists()


def test_unknown_names_are_in_message(store, journal):
    with pytest.raises(PreferenceError, match="nope"):
        put(store, journal, add={"nope": ["x"]})
    with pytest.raises(PreferenceError, match="zzz"):
        put(store, journal, scope="zzz", add={"important_keywords": ["x"]})


def test_user_request_stripped_and_500_ok(store, journal):
    put(store, journal, add={"important_keywords": ["x"]}, req=" " + "r" * 500 + " ")
    assert len(journal.entries()[0]["user_request"]) == 500


@pytest.mark.parametrize("field,op,immediate", [
    ("important_senders", "add", True), ("important_senders", "remove", False),
    ("important_keywords", "add", True), ("important_keywords", "remove", False),
    ("ignored_senders", "add", False), ("ignored_senders", "remove", True),
    ("ignored_keywords", "add", False), ("ignored_keywords", "remove", True),
])
def test_direction_rule(store, journal, field, op, immediate):
    value = "bob@x.com" if "senders" in field else "kw"
    # Seed the state so a removal has something to remove.
    with store.transaction() as s:
        s["global"][field] = [value] if op == "remove" else []
    r = put(store, journal, **{op: {field: [value]}})
    if immediate:
        assert r["applied"] and not r["pending"] and r["change_id"]
        assert store.load()["global"][field] == ([] if op == "remove" else [value])
    else:
        assert not r["applied"] and r["change_id"] is None
        assert r["pending"][0]["proposal_id"].startswith("p-") and r["pending"][0]["field"] == field
        assert store.load()["global"][field] == ([value] if op == "remove" else [])
        assert not journal.path.exists()


def test_last_triage_set_immediate_and_normalized(store, journal):
    r = put(store, journal, scope="a", triage="2026-10-30T09:00:00-03:00")
    assert r["applied"][0]["values"] == ["2026-10-30T12:00:00+00:00"]
    e = journal.get(r["change_id"])
    assert e["before"] == {"last_triage_at": None} and e["after"] == {"last_triage_at": "2026-10-30T12:00:00+00:00"}
    assert put(store, journal, scope="a", triage="2026-10-30T12:00:00Z") == {"change_id": None, "applied": [], "pending": []}


def test_journal_entry_shape_and_atomic_multi_op(store, journal):
    r = put(store, journal, scope="a", add={"important_senders": ["Bob@X.com", "bob@x.com"], "important_keywords": ["Urgent"]},
            triage="2026-10-30T11:00:00+00:00", req="user said so")
    assert r["applied"] and len(journal.entries()) == 1
    e = journal.get(r["change_id"])
    assert e["tool"] == "set_preferences" and e["kind"] == "preferences" and e["scope"] == "a"
    assert e["user_request"] == "user said so" and e["timestamp"] == T0.isoformat()
    assert e["before"]["important_senders"] == [] and e["after"]["important_senders"] == ["bob@x.com"]
    assert set(e["after"]) == {"important_senders", "important_keywords", "last_triage_at"}
    assert e["added"] == [] and e["prior"] == {}


def test_mixed_request_applies_immediate_and_proposes_rest(store, journal):
    r = put(store, journal, add={"important_keywords": ["a"], "ignored_keywords": ["b"]})
    assert [x["field"] for x in r["applied"]] == ["important_keywords"]
    assert [x["field"] for x in r["pending"]] == ["ignored_keywords"]
    assert store.load()["global"]["ignored_keywords"] == []


def test_invalid_op_in_mixed_request_blocks_everything(store, journal):
    with pytest.raises(PreferenceError):
        put(store, journal, add={"important_keywords": ["a"], "important_senders": ["bad"]})
    assert not store.path.exists()


def test_noop_request(store, journal):
    put(store, journal, add={"important_keywords": ["a"]})
    assert put(store, journal, add={"important_keywords": ["A"]}) == {"change_id": None, "applied": [], "pending": []}
    assert put(store, journal, remove={"ignored_keywords": ["zzz"]})["applied"] == []
    assert len(journal.entries()) == 1


def test_conflict_important_vs_ignored(store, journal):
    put(store, journal, add={"important_keywords": ["Invoice"]})
    with pytest.raises(PreferenceError, match="Conflict"):
        put(store, journal, add={"ignored_keywords": ["invoice"]})
    with pytest.raises(PreferenceError, match="Conflict"):
        put(store, journal, add={"important_keywords": ["x"], "ignored_keywords": ["X"]})
    # Same value in another scope is not a conflict.
    put(store, journal, scope="a", add={"ignored_keywords": ["Invoice"]})
    assert store.load()["proposals"] and len(store.load()["proposals"]) == 1


def test_conflict_via_immediate_add_against_existing_ignored(store, journal):
    with store.transaction() as s:
        s["global"]["ignored_senders"] = ["@spam.com"]
    with pytest.raises(PreferenceError, match="Conflict"):
        put(store, journal, add={"important_senders": ["@Spam.com"]})
    # Removing the ignore in the same request resolves it.
    r = put(store, journal, add={"important_senders": ["@spam.com"]}, remove={"ignored_senders": ["@spam.com"]})
    assert len(r["applied"]) == 2


def test_item_cap(store, journal):
    put(store, journal, add={"important_keywords": [f"k{i}" for i in range(200)]})
    with pytest.raises(PreferenceError, match="200"):
        put(store, journal, add={"important_keywords": ["one-more"]})
    with pytest.raises(PreferenceError, match="200"):
        put(store, journal, add={"ignored_keywords": [f"i{i}" for i in range(201)]})


def test_proposal_stored_with_expiry_and_view(store, journal):
    put(store, journal, add={"important_keywords": ["a"]})
    r = put(store, journal, scope="a", remove={"important_keywords": ["a"]}, add={"ignored_keywords": ["z"]})
    assert len(r["pending"]) == 1
    p = store.load()["proposals"][0]
    assert p["created"] == T0.isoformat() and p["expires"] == (T0 + timedelta(days=14)).isoformat()
    assert p["user_request"] == "because" and p["id"].startswith("p-") and len(p["id"]) == 10


def test_proposal_expiry(store, journal):
    r = put(store, journal, add={"ignored_keywords": ["x"]})
    pid = r["pending"][0]["proposal_id"]
    almost = T0 + timedelta(days=14) - timedelta(seconds=1)
    assert len(list_proposals(store, almost)) == 1
    late = T0 + timedelta(days=14)
    assert list_proposals(store, late) == [] and view(store, journal, None, late)["pending"] == []
    with pytest.raises(PreferenceError, match="expired"):
        approve(store, journal, pid, late)
    assert store.load()["global"]["ignored_keywords"] == []
    # Expired proposals are dropped on the next write.
    put(store, journal, add={"important_keywords": ["k"]}, now=late)
    assert store.load()["proposals"] == []


def test_approve_applies_and_journals(store, journal):
    r = put(store, journal, add={"ignored_senders": ["News@x.com"]}, req="mute news")
    pid = r["pending"][0]["proposal_id"]
    later = T0 + timedelta(hours=1)
    out = approve(store, journal, pid, later)
    assert out["applied"][0]["values"] == ["news@x.com"] and out["change_id"]
    assert store.load()["global"]["ignored_senders"] == ["news@x.com"] and store.load()["proposals"] == []
    e = journal.get(out["change_id"])
    assert e["tool"] == "approve_preference" and e["user_request"] == "mute news" and e["scope"] == "global"
    assert e["before"] == {"ignored_senders": []} and e["after"] == {"ignored_senders": ["news@x.com"]}
    with pytest.raises(PreferenceError, match="Unknown"):
        approve(store, journal, pid, later)


def test_approve_remove_proposal(store, journal):
    put(store, journal, add={"important_keywords": ["a", "b"]})
    pid = put(store, journal, remove={"important_keywords": ["A"]})["pending"][0]["proposal_id"]
    approve(store, journal, pid, T0)
    assert store.load()["global"]["important_keywords"] == ["b"]


def test_approve_revalidates_conflict_created_after_proposal(store, journal):
    pid = put(store, journal, add={"ignored_keywords": ["x"]})["pending"][0]["proposal_id"]
    put(store, journal, add={"important_keywords": ["X"]})
    with pytest.raises(PreferenceError, match="Conflict"):
        approve(store, journal, pid, T0)
    assert store.load()["global"]["ignored_keywords"] == []
    assert [p["id"] for p in store.load()["proposals"]] == [pid]  # left in place so it can be rejected


def test_approve_revalidates_cap(store, journal):
    pid = put(store, journal, add={"ignored_keywords": ["x"]})["pending"][0]["proposal_id"]
    with store.transaction() as s:
        s["global"]["ignored_keywords"] = [f"i{i}" for i in range(200)]
    with pytest.raises(PreferenceError, match="200"):
        approve(store, journal, pid, T0)


def test_approve_noop_when_already_in_state(store, journal):
    pid = put(store, journal, add={"ignored_keywords": ["x"]})["pending"][0]["proposal_id"]
    with store.transaction() as s:
        s["global"]["ignored_keywords"] = ["x"]
    out = approve(store, journal, pid, T0)
    assert out["change_id"] is None and out["applied"] == [] and not journal.path.exists()


def test_reject(store, journal):
    pid = put(store, journal, add={"ignored_keywords": ["x"]})["pending"][0]["proposal_id"]
    out = reject(store, pid, T0)
    assert out["rejected"] is True and store.load()["proposals"] == []
    assert store.load()["global"]["ignored_keywords"] == [] and not journal.path.exists()
    with pytest.raises(PreferenceError):
        reject(store, pid, T0)


def test_view_global_accounts_effective_and_recent(store, journal):
    put(store, journal, add={"important_keywords": ["g", "both"]}, now=T0)
    put(store, journal, scope="a", add={"important_keywords": ["both", "a1"]}, triage="2026-10-30T11:00:00Z", now=T0 + timedelta(minutes=1))
    put(store, journal, scope="b", add={"important_keywords": ["b1"]}, now=T0 + timedelta(minutes=2))
    put(store, journal, scope="a", add={"ignored_keywords": ["z"]}, req="mute z", now=T0 + timedelta(minutes=3))
    v = view(store, journal, None, T0 + timedelta(hours=1))
    assert set(v) == {"global", "accounts", "pending", "recent_changes"}
    assert set(v["accounts"]) == {"a", "b"} and v["global"]["last_triage_at"] is None
    assert len(v["recent_changes"]) == 3 and v["pending"][0]["user_request"] == "mute z"
    va = view(store, journal, "a", T0 + timedelta(hours=1))
    assert va["account"]["important_keywords"] == ["both", "a1"]
    assert va["effective"]["important_keywords"] == ["g", "both", "a1"]
    assert va["effective"]["last_triage_at"] == "2026-10-30T11:00:00+00:00"
    assert not any(c["scope"] == "b" for c in va["recent_changes"]) and all(p["scope"] in ("a", "global") for p in va["pending"])
    c = va["recent_changes"][-1]
    assert c["status"] == "applied" and c["user_request"] == "because" and c["undone"] is False


def test_recent_changes_since_last_triage_and_default_20(store, journal):
    for i in range(25):
        put(store, journal, add={"important_keywords": [f"k{i}"]}, now=T0 + timedelta(minutes=i))
    assert len(view(store, journal, None, T0)["recent_changes"]) == 20
    put(store, journal, triage=(T0 + timedelta(minutes=20)).isoformat(), now=T0 + timedelta(minutes=30))
    v = view(store, journal, None, T0 + timedelta(hours=2))
    assert [c["fields"] for c in v["recent_changes"]][-1] == ["important_keywords"]
    assert len(v["recent_changes"]) == 5 + 1  # k20..k24 plus the triage change itself


def test_recent_changes_status_and_undone(store, journal):
    pid = put(store, journal, add={"ignored_keywords": ["x"]})["pending"][0]["proposal_id"]
    out = approve(store, journal, pid, T0)
    r = put(store, journal, add={"important_keywords": ["k"]})
    undo(None, journal, "a", r["change_id"], store=store, now=T0)
    ch = {c["change_id"]: c for c in view(store, journal, None, T0)["recent_changes"]}
    assert ch[out["change_id"]]["status"] == "approved" and ch[r["change_id"]]["undone"] is True
    assert len(ch) == 2  # the undo entry itself is not listed


def test_atomic_save_and_corrupt_file(store, journal, tmp_path):
    put(store, journal, add={"important_keywords": ["a"]})
    assert store.lock_path == tmp_path / "preferences.lock" and store.lock_path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(RuntimeError):
        with store.transaction() as s:
            s["global"]["important_keywords"] = ["zzz"]
            raise RuntimeError
    assert store.load()["global"]["important_keywords"] == ["a"]
    store.path.write_text("{not json")
    with pytest.raises(PreferenceError):
        store.load()


def test_load_missing_file_defaults(store):
    s = store.load()
    assert s == {"global": {**{f: [] for f in prefs.LIST_FIELDS}, "last_triage_at": None}, "accounts": {}, "proposals": []}


def test_concurrent_threads_serialized(store, journal):
    def work(i):
        put(store, journal, add={"important_keywords": [f"t{i}"]})
    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(store.load()["global"]["important_keywords"]) == sorted(f"t{i}" for i in range(20))
    assert len(journal.entries()) == 20


def _proc_work(path, i):
    s = PreferenceStore(path)
    for j in range(5):
        with s.transaction() as state:
            state["global"]["important_keywords"].append(f"p{i}-{j}")


def test_concurrent_processes_serialized(store):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_proc_work, args=(store.path, i)) for i in range(6)]
    [p.start() for p in procs]
    [p.join() for p in procs]
    assert all(p.exitcode == 0 for p in procs)
    assert len(store.load()["global"]["important_keywords"]) == 30


# ----- undo / undo_since / summarize -----

def test_undo_restores_before_even_if_lowering(store, journal):
    with store.transaction() as s:
        s["global"]["ignored_keywords"] = ["x", "y"]
    r = put(store, journal, remove={"ignored_keywords": ["x"]})
    out = undo(None, journal, "a", r["change_id"], store=store, now=T0)
    assert store.load()["global"]["ignored_keywords"] == ["x", "y"]
    e = journal.get(out["change_id"])
    assert e["undo_of"] == r["change_id"] and e["kind"] == "preferences" and e["tool"] == "undo_changes"
    assert e["before"] == {"ignored_keywords": ["y"]} and e["after"] == {"ignored_keywords": ["x", "y"]}
    with pytest.raises(OrganizeError, match="already undone"):
        undo(None, journal, "a", r["change_id"], store=store, now=T0)


def test_undo_refused_if_changed_since(store, journal):
    r1 = put(store, journal, add={"important_keywords": ["a"]})
    put(store, journal, add={"important_keywords": ["b"]})
    with pytest.raises(OrganizeError, match="changed since"):
        undo(None, journal, "a", r1["change_id"], store=store)
    assert store.load()["global"]["important_keywords"] == ["a", "b"]


def test_undo_last_triage_and_refusal_atomic_across_fields(store, journal):
    r = put(store, journal, add={"important_keywords": ["a"]}, triage="2026-10-30T10:00:00Z")
    put(store, journal, triage="2026-10-30T11:00:00Z")
    with pytest.raises(OrganizeError, match="last_triage_at"):
        undo(None, journal, "a", r["change_id"], store=store)
    assert store.load()["global"]["important_keywords"] == ["a"]  # nothing partially restored


def test_undo_scope_check_and_missing_store(store, journal):
    r = put(store, journal, scope="a", add={"important_keywords": ["a"]})
    with pytest.raises(OrganizeError, match="another account"):
        undo(None, journal, "b", r["change_id"], store=store)
    with pytest.raises(OrganizeError, match="not available"):
        undo(None, journal, "a", r["change_id"])
    g = put(store, journal, add={"important_keywords": ["g"]})
    undo(None, journal, "b", g["change_id"], store=store)  # global entries are undoable from any account


def test_undo_refused_when_restore_would_conflict(store, journal):
    with store.transaction() as s:
        s["global"]["ignored_keywords"] = ["x"]
    r = put(store, journal, remove={"ignored_keywords": ["x"]})
    put(store, journal, add={"important_keywords": ["x"]})
    with pytest.raises(OrganizeError, match="Conflict"):
        undo(None, journal, "a", r["change_id"], store=store)


def test_redo_by_undoing_the_undo(store, journal):
    r = put(store, journal, add={"important_keywords": ["a"]})
    u = undo(None, journal, "a", r["change_id"], store=store)
    undo(None, journal, "a", u["change_id"], store=store)
    assert store.load()["global"]["important_keywords"] == ["a"]


def test_undo_since_preferences_newest_first_and_blocking(store, journal):
    e1 = put(store, journal, scope="a", add={"important_keywords": ["a"]}, now=T0)["change_id"]
    e2 = put(store, journal, scope="a", add={"important_keywords": ["b"]}, now=T0 + timedelta(minutes=1))["change_id"]
    e3 = put(store, journal, scope="b", add={"important_keywords": ["c"]}, now=T0 + timedelta(minutes=2))["change_id"]
    # Something else changed scope b's field since e3.
    with store.transaction() as s:
        s["accounts"]["b"]["important_keywords"].append("later")
    since = T0 - timedelta(hours=1)
    preview = undo_since(lambda a: None, journal, since, None, False, store=store)
    assert preview["total"] == 3 and preview["changes"][0]["change_id"] == e3
    assert preview["changes"][0]["scope"] == "b" and preview["changes"][0]["fields"] == ["important_keywords"]
    res = undo_since(lambda a: None, journal, since, None, True, store=store)
    assert [d["change_id"] for d in res["undone"]] == [e2, e1]
    assert list(res["failed"]) == [e3] and "changed since" in res["failed"][e3]
    assert store.load()["accounts"]["a"]["important_keywords"] == []


def test_undo_since_blocks_older_same_scope_field_after_failure(store, journal):
    older = put(store, journal, add={"important_keywords": ["a"]}, now=T0)["change_id"]
    newer = put(store, journal, add={"important_keywords": ["b"]}, now=T0 + timedelta(minutes=1))["change_id"]
    with store.transaction() as s:
        s["global"]["important_keywords"].append("later")
    other = put(store, journal, scope="a", add={"important_keywords": ["z"]}, now=T0 + timedelta(minutes=2))["change_id"]
    res = undo_since(lambda a: None, journal, T0 - timedelta(hours=1), None, True, store=store)
    assert [d["change_id"] for d in res["undone"]] == [other]
    assert set(res["failed"]) == {newer, older} and "blocked by failed newer change" in res["failed"][older]


def test_undo_since_account_filter_excludes_global_and_without_store_fails(store, journal):
    put(store, journal, add={"important_keywords": ["g"]})
    a = put(store, journal, scope="a", add={"important_keywords": ["a"]})["change_id"]
    res = undo_since(lambda x: None, journal, T0 - timedelta(hours=1), ["a"], False, store=store)
    assert [c["change_id"] for c in res["changes"]] == [a]
    res = undo_since(lambda x: None, journal, T0 - timedelta(hours=1), ["a"], True)
    assert "not available" in res["failed"][a]


def test_summarize_preference_entry(store, journal):
    r = put(store, journal, scope="a", add={"important_keywords": ["a"]}, triage="2026-10-30T10:00:00Z", req="why")
    out = summarize_entry(journal.get(r["change_id"]), False, {})
    assert out == {
        "change_id": r["change_id"], "timestamp": T0.isoformat(), "tool": "set_preferences",
        "kind": "preferences", "scope": "a", "fields": ["important_keywords", "last_triage_at"],
        "user_request": "why", "undone": False, "undo_of": None,
    }
