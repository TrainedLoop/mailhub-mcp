"""Behavior every MailProvider must satisfy. Subclass and define `provider(self, seed)`."""

import pytest

from mailhub_mcp.core.models import Draft, Label, LabelState, Message, MessageSummary, Thread

ATTRIBUTES = [
    "name", "protected_labels", "inbox_label", "unread_label", "starred_label",
    "default_inbox_query", "query_hint", "reauthorize_hint",
]


def default_seed():
    def msg(id, thread_id, labels, date, body):
        return {
            "id": id, "thread_id": thread_id, "labels": labels,
            "from": "ana@example.com", "subject": f"subject {id}", "body": body,
            "internal_date": date, "message_id_header": f"<{id}@example.com>",
        }

    return {
        "messages": [
            msg("m1", "t1", ["INBOX", "UNREAD"], 1000, "first body"),
            msg("m2", "t1", ["INBOX"], 2000, "second body"),
            msg("m3", "t3", ["INBOX"], 3000, "third body"),
        ],
        "labels": ["Existing"],
        "transient_ids": ["t-fail"],
    }


def _ids(seed) -> set[str]:
    return {m["id"] for m in seed["messages"]}


class ProviderContract:
    @pytest.fixture
    def seed(self):
        return default_seed()

    def test_declares_required_attributes(self, provider):
        for attr in ATTRIBUTES:
            assert hasattr(type(provider), attr), attr
        assert isinstance(provider.protected_labels, frozenset)

    def test_address(self, provider):
        assert "@" in provider.address()

    def test_search_returns_summaries(self, provider, seed):
        results = provider.search(provider.default_inbox_query, 10)
        assert results and all(isinstance(r, MessageSummary) for r in results)
        assert {r.id for r in results} <= _ids(seed)

    def test_search_report_matches_search_and_counts_unreadable(self, provider, seed):
        report = provider.search_report(provider.default_inbox_query, 10)
        assert [m.id for m in report.found] == [
            m.id for m in provider.search(provider.default_inbox_query, 10)
        ]
        assert report.unreadable == len(seed["transient_ids"])

    def test_list_ids(self, provider, seed):
        assert set(provider.list_ids(provider.default_inbox_query, 10)) == _ids(seed) | set(
            seed["transient_ids"]
        )

    def test_list_ids_filters_by_label_ids(self, provider, seed):
        query = provider.default_inbox_query
        assert provider.list_ids(query, 10, label_ids=None) == provider.list_ids(query, 10)
        unread = [m["id"] for m in seed["messages"] if provider.unread_label in m["labels"]]
        assert provider.list_ids(query, 10, label_ids=[provider.unread_label]) == unread
        both = [provider.inbox_label, provider.unread_label]
        assert provider.list_ids(query, 10, label_ids=both) == unread  # messages carrying every label
        assert provider.list_ids(query, 10, label_ids=[provider.starred_label]) == []

    def test_get_messages_splits_found_missing_failed(self, provider, seed):
        first, transient = seed["messages"][0]["id"], seed["transient_ids"][0]
        r = provider.get_messages([first, "nope", transient], "labels")
        assert [x.id for x in r.found] == [first]
        assert r.missing == ["nope"]
        assert list(r.failed) == [transient]
        assert all(isinstance(x, LabelState) for x in r.found)

    def test_every_requested_id_lands_in_exactly_one_group(self, provider, seed):
        ids = [*_ids(seed), "nope", *seed["transient_ids"]]
        r = provider.get_messages(ids, "labels")
        groups = [x.id for x in r.found] + list(r.missing) + list(r.failed)
        assert sorted(groups) == sorted(ids)

    def test_get_messages_summary(self, provider, seed):
        m = seed["messages"][0]
        r = provider.get_messages([m["id"]], "summary")
        assert isinstance(r.found[0], MessageSummary) and r.found[0].subject == m["subject"]

    def test_get_messages_full_has_body(self, provider, seed):
        m = seed["messages"][0]
        r = provider.get_messages([m["id"]], "full")
        assert isinstance(r.found[0], Message) and r.found[0].body == m["body"]

    def test_get_thread(self, provider, seed):
        thread_id = seed["messages"][0]["thread_id"]
        t = provider.get_thread(thread_id)
        assert isinstance(t, Thread)
        expected = [m["id"] for m in seed["messages"] if m["thread_id"] == thread_id]
        assert [m.id for m in t.messages] == expected

    def test_list_labels(self, provider, seed):
        labels = provider.list_labels()
        assert all(isinstance(l, Label) for l in labels)
        assert set(seed["labels"]) <= {l.name for l in labels}

    def test_ensure_label_case_insensitive_no_duplicate(self, provider):
        a = provider.ensure_label("Shopping")
        b = provider.ensure_label("shopping")
        assert a.id == b.id
        assert sum(l.name.lower() == "shopping" for l in provider.list_labels()) == 1

    def test_modify_labels_roundtrip(self, provider, seed):
        mid = seed["messages"][0]["id"]
        label = provider.ensure_label("Shopping")
        provider.modify_labels([mid], [label.id], [])
        assert label.id in provider.get_messages([mid], "labels").found[0].labels
        provider.modify_labels([mid], [], [label.id])
        assert label.id not in provider.get_messages([mid], "labels").found[0].labels

    def test_list_drafts_after_create(self, provider):
        draft = provider.create_draft("b@example.com", "hello", "body", None, None)
        assert draft.draft_id in {d.draft_id for d in provider.list_drafts(10)}

    def test_reply_draft_threads(self, provider, seed):
        m = seed["messages"][0]
        draft = provider.create_draft(m["from"], "", "reply", None, m["id"])
        assert isinstance(draft, Draft) and draft.thread_id == m["thread_id"]

    def test_quota_status_when_provided(self, provider):
        status = getattr(provider, "quota_status", None)
        if status is None:  # optional: providers without a quota budget omit it
            return
        before = status()
        assert set(before) == {"used_last_minute", "budget_per_minute"}
        assert all(type(v) is int and v >= 0 for v in before.values())
        provider.get_messages(["m1"], "labels")
        assert status()["used_last_minute"] > before["used_last_minute"]
