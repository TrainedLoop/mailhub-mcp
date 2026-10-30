from unittest.mock import ANY

import pytest

from mailhub_mcp.core.errors import OrganizeError
from mailhub_mcp.core.journal import Journal
from mailhub_mcp.core.organize import apply_query, modify, preview_query, undo
from mailhub_mcp.providers.gmail.provider import GmailProvider
from tests.fakes.fake_gmail import FakeGmail, http_error


@pytest.fixture
def fake():
    f = FakeGmail()
    f.labels.append({"id": "Label_9", "name": "Shopping", "type": "user"})
    f.add_message("m1", labels=["INBOX", "UNREAD", "STARRED"])
    f.add_message("m2", labels=["INBOX", "UNREAD"])
    return f


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    monkeypatch.setattr(
        "mailhub_mcp.providers.gmail.provider.Credentials.from_authorized_user_file",
        lambda *a, **k: object(),
    )


@pytest.fixture
def provider(fake, tmp_path):
    return GmailProvider("a", tmp_path / "a.json", build_service=lambda c: fake)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "c.jsonl")


def org_label_id(fake):
    return next(l["id"] for l in fake.labels if l["name"] == "claude/organized")


def no_writes(fake):
    return not any(c[0] in ("messages.batchModify", "labels.create") for c in fake.calls)


def test_archive_mark_read_star_and_org_label(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1"], [], [], archive=True, mark_read=True, star=True)
    labels = fake.messages["m1"]["labelIds"]
    assert "INBOX" not in labels and "UNREAD" not in labels and "STARRED" in labels
    assert org_label_id(fake) in labels and r["modified"] == 1


@pytest.mark.parametrize("label", ["TRASH", "spam", "SENT", "DRAFT", "CHAT"])
def test_blocked_labels_refused_without_writes(fake, provider, journal, label):
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m1"], [label], [], False, None, None)
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m1"], [], [label], False, None, None)
    assert no_writes(fake)


def test_label_name_case_insensitive_and_by_id(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], ["shopping"], [], False, None, None)
    assert "Label_9" in fake.messages["m1"]["labelIds"]
    modify(provider, journal, "a", ["m2"], ["Label_9"], [], False, None, None)
    assert "Label_9" in fake.messages["m2"]["labelIds"]


def test_unknown_label_refused(fake, provider, journal):
    with pytest.raises(OrganizeError, match="Nope"):
        modify(provider, journal, "a", ["m1"], ["Nope"], [], False, None, None)
    assert no_writes(fake)


def test_ids_from_other_account_refused_atomically(fake, provider, journal):
    with pytest.raises(OrganizeError, match="not found"):
        modify(provider, journal, "a", ["m1", "nope"], [], [], True, None, None)
    assert "INBOX" in fake.messages["m1"]["labelIds"]
    assert no_writes(fake)


def test_empty_ids_and_no_change_refused(fake, provider, journal):
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", [], [], [], True, None, None)
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m1"], [], [], False, None, None)
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", [f"m{i}" for i in range(1001)], [], [], True, None, None)
    assert no_writes(fake)


def test_same_label_in_add_and_remove_refused(fake, provider, journal):
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m1"], ["UNREAD"], [], False, True, None)
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m1"], ["shopping"], ["Label_9"], False, None, None)
    assert no_writes(fake)


def test_removing_org_label_conflicts(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    with pytest.raises(OrganizeError):
        modify(provider, journal, "a", ["m2"], [], ["claude/organized"], False, None, None)


def test_prior_records_only_touched_labels(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1"], [], [], archive=True, mark_read=None, star=True)
    assert journal.get(r["change_id"])["prior"] == {"m1": ["INBOX", "STARRED"]}


def test_preview_does_not_modify(fake, provider):
    fake.query_results["from:shop"] = [f"m{i}" for i in range(600)]
    for i in range(600):
        fake.add_message(f"m{i}", labels=["INBOX"])
    p = preview_query(provider, "from:shop")
    assert p == {"matched": 501, "capped": True, "sample": ANY} and len(p["sample"]) == 10
    assert no_writes(fake)


def test_apply_query_caps_at_500_and_reports_remaining(fake, provider, journal):
    ids = [f"q{i}" for i in range(600)]
    for i in ids:
        fake.add_message(i, labels=["INBOX"])
    fake.query_results["from:shop"] = ids
    r = apply_query(provider, journal, "a", "from:shop", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r["modified"] == 500 and r["remaining"] is True
    assert journal.get(r["change_id"])["tool"] == "modify_by_query"
    assert "INBOX" not in fake.messages["q0"]["labelIds"]
    assert "INBOX" in fake.messages["q599"]["labelIds"]


def test_apply_query_zero_matches_no_journal(fake, provider, journal, tmp_path):
    fake.query_results["none"] = []
    r = apply_query(provider, journal, "a", "none", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r == {"change_id": None, "modified": 0, "remaining": False}
    assert not (tmp_path / "c.jsonl").exists() or (tmp_path / "c.jsonl").read_text() == ""


def test_undo_restores_exact_state(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1", "m2"], [], [], archive=True, mark_read=None, star=True)
    u = undo(provider, journal, "a", r["change_id"])
    assert u["undone"] == r["change_id"] and u["modified"] == 2
    m1, m2 = fake.messages["m1"]["labelIds"], fake.messages["m2"]["labelIds"]
    assert "STARRED" in m1 and "STARRED" not in m2
    assert "INBOX" in m1 and "INBOX" in m2
    assert org_label_id(fake) not in m1 + m2
    assert journal.get(u["change_id"])["undo_of"] == r["change_id"]


def test_undo_twice_refused(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    undo(provider, journal, "a", r["change_id"])
    with pytest.raises(OrganizeError):
        undo(provider, journal, "a", r["change_id"])


def test_undo_of_a_label_undo_entry_is_refused(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1"], ["Shopping"], [], False, None, None, max_per_day=1)
    u = undo(provider, journal, "a", r["change_id"])
    before = {k: list(v["labelIds"]) for k, v in fake.messages.items()}
    # Redoing through undo-of-undo would also bypass the daily cap (undo entries are not counted).
    with pytest.raises(OrganizeError, match="this change is an undo; to redo, make the change again"):
        undo(provider, journal, "a", u["change_id"])
    assert {k: v["labelIds"] for k, v in fake.messages.items()} == before
    assert len(journal.entries()) == 2


def test_undo_other_account_and_unknown_refused(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    with pytest.raises(OrganizeError):
        undo(provider, journal, "b", r["change_id"])
    with pytest.raises(OrganizeError):
        undo(provider, journal, "a", "missing")
    assert "INBOX" not in fake.messages["m1"]["labelIds"]


def test_undo_transient_read_error_refused_then_retry_succeeds(fake, provider, journal):

    r = modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    fake.get_errors["m2"] = http_error(503, "Unavailable")
    fake.calls.clear()
    with pytest.raises(OrganizeError, match="m2.*retry"):
        undo(provider, journal, "a", r["change_id"])
    assert no_writes(fake) and not journal.is_undone(r["change_id"])
    assert "INBOX" not in fake.messages["m1"]["labelIds"]
    del fake.get_errors["m2"]
    u = undo(provider, journal, "a", r["change_id"])
    assert u["modified"] == 2 and u["skipped"] == []
    assert "INBOX" in fake.messages["m1"]["labelIds"] and "INBOX" in fake.messages["m2"]["labelIds"]


def test_undo_skips_missing_messages_and_reports_them(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    del fake.messages["m2"]
    u = undo(provider, journal, "a", r["change_id"])
    assert u["skipped"] == ["m2"] and u["modified"] == 1
    assert "INBOX" in fake.messages["m1"]["labelIds"]


def test_modify_transient_read_failure_refused_without_writes(fake, provider, journal):

    fake.get_errors["m2"] = http_error(503, "Unavailable")
    with pytest.raises(OrganizeError, match="retry"):
        modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    assert no_writes(fake)


def test_protected_labels_come_from_the_provider(journal):
    from mailhub_mcp.core.models import Label

    class Stub:
        protected_labels = frozenset({"JUNK"})
        inbox_label, unread_label, starred_label = "INBOX", "UNREAD", "STARRED"

        def list_labels(self):
            return [Label(id="Label_1", name="Junk", type="user")]

        def get_messages(self, ids, detail):
            raise AssertionError("must refuse before reading")

    with pytest.raises(OrganizeError, match="cannot be changed"):
        modify(Stub(), journal, "a", ["m1"], ["junk"], [], False, None, None)


class _OmittingProvider:
    """Wraps a real provider but silently drops `omit` from get_messages results."""

    def __init__(self, inner, omit):
        self._inner, self.omit, self.writes = inner, omit, []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_messages(self, ids, detail):
        return self._inner.get_messages([i for i in ids if i not in self.omit], detail)

    def ensure_label(self, name):
        self.writes.append("ensure_label")
        return self._inner.ensure_label(name)

    def modify_labels(self, ids, add, remove):
        self.writes.append("modify_labels")
        return self._inner.modify_labels(ids, add, remove)


def test_modify_refuses_unaccounted_id_without_any_write(fake, provider, journal, tmp_path):
    stub = _OmittingProvider(provider, {"m2"})
    with pytest.raises(OrganizeError, match="m2"):
        modify(stub, journal, "a", ["m1", "m2"], [], [], True, None, None)
    assert stub.writes == [] and no_writes(fake)
    assert not (tmp_path / "c.jsonl").exists()


def test_undo_refuses_unaccounted_id_and_stays_undoable(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    stub = _OmittingProvider(provider, {"m2"})
    with pytest.raises(OrganizeError, match="m2"):
        undo(stub, journal, "a", r["change_id"])
    assert stub.writes == [] and not journal.is_undone(r["change_id"])
    assert "INBOX" not in fake.messages["m1"]["labelIds"]
    assert undo(provider, journal, "a", r["change_id"])["modified"] == 2


def test_journal_failure_after_write_logs_full_entry_at_error(fake, provider, journal, caplog, monkeypatch):
    import logging

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "record", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError):
            modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "m1" in errors[0] and "INBOX" in errors[0] and "prior" in errors[0]
    assert "INBOX" not in fake.messages["m1"]["labelIds"]


def test_apply_query_skips_messages_already_in_target_state(fake, provider, journal):
    fake.add_message("m3", labels=[])
    fake.query_results["q"] = ["m1", "m2", "m3"]
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r["modified"] == 2
    assert sorted(journal.get(r["change_id"])["prior"]) == ["m1", "m2"]


def test_apply_query_all_already_changed_returns_hint_without_journal(fake, provider, journal, tmp_path):
    fake.query_results["q"] = ["m1", "m2"]
    apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                archive=True, mark_read=None, star=None)
    fake.calls.clear()
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r == {
        "change_id": None, "modified": 0, "remaining": False,
        "hint": "query still matches messages already changed; refine the query",
    }
    assert no_writes(fake)
    assert len((tmp_path / "c.jsonl").read_text().splitlines()) == 1


def test_concurrent_undo_of_same_change_succeeds_once(fake, provider, journal):
    import threading
    import time

    r = modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    real = provider.modify_labels

    def slow(*a, **k):
        time.sleep(0.05)
        return real(*a, **k)

    provider.modify_labels = slow
    outcomes = []

    def run():
        try:
            outcomes.append(undo(provider, journal, "a", r["change_id"]))
        except OrganizeError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errors = [o for o in outcomes if isinstance(o, OrganizeError)]
    assert len(errors) == 1 and "already undone" in str(errors[0])
    assert sum(1 for o in outcomes if isinstance(o, dict)) == 1
    undo_entries = [e for e in journal._entries() if e.get("undo_of") == r["change_id"]]
    assert len(undo_entries) == 1


def test_apply_query_all_already_changed_reports_remaining_when_capped(fake, provider, journal):
    from mailhub_mcp.core.organize import MAX_QUERY_APPLY

    ids = [f"x{i}" for i in range(MAX_QUERY_APPLY + 1)]
    for i in ids:
        fake.add_message(i, labels=[])
    fake.query_results["q"] = ids
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r["change_id"] is None and r["modified"] == 0
    assert r["remaining"] is True


def test_journal_failure_after_noop_undo_does_not_claim_labels_changed(
    fake, provider, journal, caplog, monkeypatch
):
    import logging

    r = modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    del fake.messages["m1"], fake.messages["m2"]

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "record", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError):
            undo(provider, journal, "a", r["change_id"])
    errors = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
    assert errors
    assert "no label changes" in errors[0]
    assert "after labels were changed" not in errors[0]


def test_modify_refusal_for_unreadable_ids_does_not_claim_failure_is_temporary(fake, provider, journal):
    fake.get_errors["m1"] = http_error(403, "Forbidden")
    with pytest.raises(OrganizeError, match=r"Could not read message labels \(retry if the failure is temporary\).*m1"):
        modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    assert no_writes(fake)


# --- daily change cap ---

from datetime import datetime, timedelta, timezone  # noqa: E402


def _seed(journal, account, count, age_hours=1.0, undo_of=None):
    """Append a journal entry for `count` messages, `age_hours` old."""
    journal.record(account, "modify_emails", ["Label_9"], [], {f"x{i}": [] for i in range(count)}, undo_of=undo_of)
    lines = journal.path.read_text().splitlines()
    import json
    entry = json.loads(lines[-1])
    entry["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    lines[-1] = json.dumps(entry)
    journal.path.write_text("\n".join(lines) + "\n")


def test_cap_refuses_before_any_write_and_states_remaining(fake, provider, journal):
    _seed(journal, "a", 9)
    before = len(fake.calls)
    with pytest.raises(OrganizeError, match=r"only 1 of 10"):
        modify(provider, journal, "a", ["m1", "m2"], ["Shopping"], [], False, None, None, max_per_day=10)
    assert no_writes(fake) and not any(c[0] == "messages.batchModify" for c in fake.calls[before:])
    assert len(journal.entries()) == 1


def test_cap_allows_up_to_the_limit(fake, provider, journal):
    _seed(journal, "a", 8)
    r = modify(provider, journal, "a", ["m1", "m2"], ["Shopping"], [], False, None, None, max_per_day=10)
    assert r["modified"] == 2


def test_cap_window_rolls(fake, provider, journal):
    _seed(journal, "a", 50, age_hours=25)
    r = modify(provider, journal, "a", ["m1", "m2"], ["Shopping"], [], False, None, None, max_per_day=10)
    assert r["modified"] == 2


def test_cap_is_per_account(fake, provider, journal):
    _seed(journal, "other", 50)
    r = modify(provider, journal, "a", ["m1"], ["Shopping"], [], False, None, None, max_per_day=10)
    assert r["modified"] == 1


def test_undo_entries_not_counted_and_undo_not_capped(fake, provider, journal):
    r = modify(provider, journal, "a", ["m1", "m2"], ["Shopping"], [], False, None, None, max_per_day=2)
    undone = undo(provider, journal, "a", r["change_id"])  # cap of 2 is exhausted; undo still works
    assert undone["modified"] == 2
    from mailhub_mcp.core.organize import changes_in_window
    assert changes_in_window(journal, "a") == 2  # the undo entry adds nothing


def test_apply_query_respects_cap(fake, provider, journal):
    fake.query_results["in:inbox"] = ["m1", "m2"]
    _seed(journal, "a", 1)
    with pytest.raises(OrganizeError, match=r"^2 message\(s\) requested would exceed the daily change cap: only 1 of 2"):
        apply_query(provider, journal, "a", "in:inbox", max_per_day=2, add_labels=["Shopping"],
                    remove_labels=[], archive=False, mark_read=None, star=None)
    assert no_writes(fake)


def test_labels_are_read_under_the_journal_lock(fake, provider, journal):
    """`prior` must be read inside the lock that serializes writes, or it can go stale."""
    seen = []
    real = provider.get_messages

    def spy(ids, fmt):
        seen.append(journal.lock.locked())
        return real(ids, fmt)

    provider.get_messages = spy
    modify(provider, journal, "a", ["m1"], [], [], archive=True, mark_read=None, star=None)
    assert seen == [True]


def test_label_journal_entries_have_no_preference_keys(fake, provider, journal):
    modify(provider, journal, "a", ["m1"], [], [], archive=True, mark_read=None, star=None)
    assert "kind" not in journal.entries()[0] and "scope" not in journal.entries()[0]


def _minimal_reads(fake):
    return [c for c in fake.calls if c[0] == "messages.get" and c[1].get("format") == "minimal"]


def _gets(fake):
    return [c for c in fake.calls if c[0] == "messages.get"]


def _lists(fake):
    return [c for c in fake.calls if c[0] == "messages.list"]


def test_apply_query_reads_label_state_by_listing_not_per_message(fake, provider, journal):
    fake.add_message("m3", labels=[])
    fake.query_results["q"] = ["m1", "m2", "m3"]
    org = provider.ensure_label("claude/organized")
    fake.calls.clear()
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r["modified"] == 2 and _gets(fake) == []
    # touched labels INBOX and the organized label: one listing each, plus the query and its re-check
    label_lists = sorted(tuple(c[1]["labelIds"]) for c in _lists(fake) if "labelIds" in c[1])
    assert label_lists == sorted([("INBOX",), (org.id,)])
    assert len(_lists(fake)) == 2 + 2
    assert journal.get(r["change_id"])["prior"] == {"m1": ["INBOX"], "m2": ["INBOX"]}


def test_apply_query_all_already_changed_lists_only_and_writes_nothing(fake, provider, journal):
    fake.query_results["q"] = ["m1", "m2"]
    apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                archive=True, mark_read=None, star=None)
    fake.calls.clear()
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert r["modified"] == 0 and "hint" in r
    assert _gets(fake) == [] and no_writes(fake)


def test_apply_query_lists_under_the_journal_lock(fake, provider, journal):
    fake.query_results["q"] = ["m1", "m2"]
    seen = []
    real = provider.list_ids

    def spy(query, limit, label_ids=None):
        seen.append(journal.lock.locked())
        return real(query, limit, label_ids=label_ids)

    provider.list_ids = spy
    apply_query(provider, journal, "a", "q", add_labels=["Shopping"], remove_labels=[],
                archive=False, mark_read=None, star=None)
    assert seen and all(seen)


def test_apply_query_prior_includes_existing_org_label_and_undo_is_exact(fake, provider, journal):
    org = provider.ensure_label("claude/organized")
    fake.add_message("m3", labels=["INBOX", org.id, "Label_9"])
    fake.add_message("m4", labels=[org.id])
    fake.query_results["q"] = ["m1", "m2", "m3", "m4"]
    before = {i: list(fake.messages[i]["labelIds"]) for i in ("m1", "m2", "m3", "m4")}
    r = apply_query(provider, journal, "a", "q", add_labels=["Shopping"], remove_labels=[],
                    archive=True, mark_read=False, star=None)
    prior = {i: sorted(v) for i, v in journal.get(r["change_id"])["prior"].items()}
    assert prior == {
        "m1": ["INBOX", "UNREAD"], "m2": ["INBOX", "UNREAD"],
        "m3": sorted(["Label_9", "INBOX", org.id]), "m4": [org.id],
    }
    undo(provider, journal, "a", r["change_id"])
    after = {i: fake.messages[i]["labelIds"] for i in before}
    assert {i: sorted(v) for i, v in after.items()} == {i: sorted(v) for i, v in before.items()}


def test_apply_query_skip_uses_listed_state(fake, provider, journal):
    fake.add_message("m3", labels=["STARRED"])
    fake.query_results["q"] = ["m1", "m2", "m3"]
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=False, mark_read=None, star=True)
    assert r["modified"] == 1 and list(journal.get(r["change_id"])["prior"]) == ["m2"]
    assert journal.get(r["change_id"])["prior"]["m2"] == []


def test_apply_query_falls_back_to_reads_when_matches_change_meanwhile(fake, provider, journal):
    fake.add_message("m3", labels=["INBOX"])
    fake.query_results["q"] = ["m1", "m2"]
    real = provider.list_ids
    calls = []

    def racing(query, limit, label_ids=None):
        out = real(query, limit, label_ids=label_ids)
        calls.append(label_ids)
        if label_ids is not None:  # new mail arrives while label state is being listed
            fake.query_results["q"] = ["m3", "m1", "m2"]
        return out

    provider.list_ids = racing
    fake.calls.clear()
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=True, mark_read=None, star=None)
    assert {c[1]["id"] for c in _gets(fake)} == {"m1", "m2"}  # exact per-message reads of the batch
    assert r["modified"] == 2
    assert journal.get(r["change_id"])["prior"] == {"m1": ["INBOX"], "m2": ["INBOX"]}


def test_apply_query_falls_back_when_label_listing_order_is_inconsistent(fake, provider, journal):
    ids = ["m1", "m2"]
    fake.add_message("old", labels=["INBOX", "UNREAD"])
    fake.query_results["q"] = ids
    real = provider.list_ids

    def odd(query, limit, label_ids=None):
        if label_ids == ["UNREAD"]:  # a full listing with a foreign id ahead of ours
            return ["old", "m1"][:limit]
        return real(query, limit, label_ids=label_ids)

    provider.list_ids = odd
    fake.calls.clear()
    r = apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=False, mark_read=True, star=None)
    assert {c[1]["id"] for c in _gets(fake)} == {"m1", "m2"}
    assert journal.get(r["change_id"])["prior"] == {"m1": ["UNREAD"], "m2": ["UNREAD"]}


def test_apply_query_validation_errors_write_nothing(fake, provider, journal):
    fake.query_results["q"] = ["m1"]
    with pytest.raises(OrganizeError, match="Unknown label"):
        apply_query(provider, journal, "a", "q", add_labels=["Nope"], remove_labels=[],
                    archive=False, mark_read=None, star=None)
    with pytest.raises(OrganizeError, match="No change requested"):
        apply_query(provider, journal, "a", "q", add_labels=[], remove_labels=[],
                    archive=False, mark_read=None, star=None)
    assert no_writes(fake)


def _rate_limit_error():
    return http_error(429, "Too many concurrent requests for user")


def test_modify_rate_limited_read_gives_advice_and_writes_nothing(fake, provider, journal):
    from mailhub_mcp.core.errors import RateLimited

    fake.get_error_seq["m1"] = [_rate_limit_error() for _ in range(9)]
    with pytest.raises(RateLimited, match=r"^a: Gmail rate limit reached for this mailbox; wait about 30 seconds"):
        modify(provider, journal, "a", ["m1", "m2"], [], [], True, None, None)
    assert no_writes(fake) and journal.entries() == []


def test_undo_rate_limited_read_gives_advice_and_stays_undoable(fake, provider, journal):
    from mailhub_mcp.core.errors import RateLimited

    r = modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    fake.get_error_seq["m1"] = [_rate_limit_error() for _ in range(9)]
    with pytest.raises(RateLimited):
        undo(provider, journal, "a", r["change_id"])
    assert undo(provider, journal, "a", r["change_id"])["modified"] == 1


def test_undo_since_reports_rate_limit_advice_without_double_alias(fake, provider, journal):
    from datetime import datetime, timezone

    from mailhub_mcp.core.organize import undo_since

    r = modify(provider, journal, "a", ["m1"], [], [], True, None, None)
    fake.get_error_seq["m1"] = [_rate_limit_error() for _ in range(9)]
    out = undo_since(lambda alias: provider, journal, datetime(2000, 1, 1, tzinfo=timezone.utc), None, True)
    assert out["failed"][r["change_id"]].startswith("a: Gmail rate limit reached for this mailbox; wait about")
