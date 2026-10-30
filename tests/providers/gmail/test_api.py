import base64

import pytest

from mailhub_mcp.providers.gmail.api import (
    batch_get,
    create_draft,
    get_message,
    get_thread,
    list_drafts,
    list_ids,
    list_labels,
    search,
)
from mailhub_mcp.providers.gmail.parsing import summarize
from tests.fakes.fake_gmail import FakeGmail, http_error


@pytest.fixture
def fake():
    return FakeGmail()


def test_summary_triage_signals(fake):
    fake.add_message(
        "m1",
        labels=["UNREAD", "IMPORTANT", "CATEGORY_PROMOTIONS"],
        headers={"From": "a@b.com", "List-Unsubscribe": "<mailto:u@b.com>"},
        internal_date=1700,
    )
    s = summarize(fake.messages["m1"])
    assert (s["unread"], s["is_important"], s["category"], s["is_newsletter"], s["internal_date"]) == (
        True, True, "promotions", True, 1700,
    )
    assert s["starred"] is False


def test_summary_no_category_no_newsletter(fake):
    fake.add_message("m1", labels=["INBOX", "STARRED"], headers={"Subject": "Hi"})
    s = summarize(fake.messages["m1"])
    assert (s["category"], s["is_newsletter"], s["unread"], s["starred"]) == (None, False, False, True)
    assert s["subject"] == "Hi"


def test_search_returns_summaries_via_batch(fake):
    fake.add_message("m1", headers={"Subject": "one"})
    fake.add_message("m2", headers={"Subject": "two"})
    fake.query_results["is:unread"] = ["m2", "m1"]
    results = search(fake, "is:unread", 20)
    assert [r["id"] for r in results] == ["m2", "m1"]
    assert results[0]["subject"] == "two"
    assert ("messages.get", {"userId": "me", "id": "m2", "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date", "List-Unsubscribe"]}) in fake.calls


@pytest.fixture(autouse=True)
def _sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr("mailhub_mcp.providers.gmail.api._sleep", slept.append)
    return slept


def test_batch_get_chunks_of_50(fake):
    fake.batch_limit = 50
    for i in range(250):
        fake.add_message(f"m{i}")
    msgs, errors = batch_get(fake, [f"m{i}" for i in range(250)], "minimal")
    assert len(msgs) == 250 and errors == {}
    assert "payload" not in msgs[0]


def test_get_message_has_cc_body_attachments(fake):
    fake.add_message("m1", headers={"Cc": "c@d.com"}, body="text")
    m = get_message(fake, "m1")
    assert (m["cc"], m["body"], m["attachments"]) == ("c@d.com", "text", [])


def test_html_body_extracted_and_truncated(fake):
    fake.add_message("m1", html="<p>Héllo wörld</p><script>x()</script><div>line 2</div>")
    assert get_message(fake, "m1")["body"] == "Héllo wörld\nline 2"
    fake.add_message("m2", body="x" * 25_000)
    assert get_message(fake, "m2")["body"].endswith("[... truncated ...]")


def test_thread_replied_by_me(fake):
    fake.add_message("m1", thread_id="t1", labels=["INBOX"])
    fake.add_message("m2", thread_id="t1", labels=["SENT"])
    fake.add_message("m3", thread_id="t2", labels=["INBOX"])
    t = get_thread(fake, "t1")
    assert [m["id"] for m in t["messages"]] == ["m1", "m2"]
    assert t["replied_by_me"] is True
    assert get_thread(fake, "t2")["replied_by_me"] is False


def test_list_ids_paginates(fake):
    fake.page_size = 3
    for i in range(7):
        fake.add_message(f"m{i}", internal_date=i)
    assert list_ids(fake, "", 5) == ["m6", "m5", "m4", "m3", "m2"]
    assert len([c for c in fake.calls if c[0] == "messages.list"]) == 2
    assert len(list_ids(fake, "", 50)) == 7


def test_list_labels(fake):
    assert {"id": "INBOX", "name": "INBOX", "type": "system"} in list_labels(fake)


def test_list_drafts(fake):
    d = create_draft(fake, "x@y.com", "Subject", "hi", None, None)
    drafts = list_drafts(fake, 10)
    assert [x["draft_id"] for x in drafts] == [d["draft_id"]]
    assert drafts[0]["subject"] == "Subject"


def test_reply_draft_headers(fake):
    fake.add_message("m1", thread_id="t1", headers={"Subject": "Order", "Message-ID": "<a@b>"})
    d = create_draft(fake, "x@y.com", "", "hi", None, "m1")
    raw = base64.urlsafe_b64decode(fake.drafts[d["draft_id"]]["message"]["raw"]).decode()
    assert "In-Reply-To: <a@b>" in raw and "Subject: Re: Order" in raw
    assert d["thread_id"] == "t1"


def _get_calls(fake, id):
    return [c for c in fake.calls if c[0] == "messages.get" and c[1]["id"] == id]


def test_batch_get_retries_429_then_succeeds(fake, _sleeps):

    fake.add_message("m1")
    fake.get_error_seq["m1"] = [http_error(429, "Rate")]
    msgs, errors = batch_get(fake, ["m1"], "minimal")
    assert [m["id"] for m in msgs] == ["m1"] and errors == {}
    assert len(_get_calls(fake, "m1")) == 2 and _sleeps == [1]


def test_batch_get_gives_up_after_three_attempts_on_5xx(fake, _sleeps):

    fake.add_message("m1")
    fake.add_message("m2")
    fake.get_error_seq["m1"] = [http_error(503, "Unavailable") for _ in range(5)]
    msgs, errors = batch_get(fake, ["m1", "m2"], "minimal")
    assert [m["id"] for m in msgs] == ["m2"] and list(errors) == ["m1"]
    assert len(_get_calls(fake, "m1")) == 3 and len(_get_calls(fake, "m2")) == 1
    assert _sleeps == [1, 2]


def test_batch_get_never_retries_404(fake, _sleeps):
    msgs, errors = batch_get(fake, ["nope"], "minimal")
    assert msgs == [] and list(errors) == ["nope"]
    assert len(_get_calls(fake, "nope")) == 1 and _sleeps == []


def _add_drafts(fake, n):
    for i in range(n):
        create_draft(fake, f"u{i}@x.com", f"s{i}", "b", None, None)


def test_list_drafts_fetches_details_in_one_batch(fake):
    _add_drafts(fake, 5)
    fake.calls.clear()
    drafts = list_drafts(fake, 10)
    assert [d["subject"] for d in drafts] == [f"s{i}" for i in range(5)]
    assert [c for c in fake.calls if c[0] == "batch.execute"] == [("batch.execute", {"size": 5})]
    assert len([c for c in fake.calls if c[0] == "drafts.get"]) == 5  # queued in the batch, no extra round trips


def test_list_drafts_paginates_up_to_max_results(fake):
    fake.page_size = 2
    _add_drafts(fake, 5)
    assert len(list_drafts(fake, 10)) == 5
    fake.calls.clear()
    assert len(list_drafts(fake, 3)) == 3
    assert len([c for c in fake.calls if c[0] == "drafts.list"]) == 2


def test_list_drafts_skips_draft_deleted_meanwhile_and_raises_other_errors(fake):
    _add_drafts(fake, 3)
    first, second, _ = fake.drafts
    fake.draft_errors[first] = http_error(404, "gone")
    assert [d["draft_id"] for d in list_drafts(fake, 10)] == [second, list(fake.drafts)[2]]
    fake.draft_errors[second] = http_error(403, "forbidden")
    with pytest.raises(Exception, match="forbidden"):
        list_drafts(fake, 10)
