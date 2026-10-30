import pytest

from mailhub_mcp.core.models import (
    Attachment,
    Draft,
    DraftSummary,
    Label,
    LabelState,
    Message,
    MessageSummary,
    Thread,
    with_account,
)

base = dict(
    id="m1", thread_id="t1", to="", subject="", date="", snippet="", labels=[],
    internal_date=1, unread=False, starred=False, is_important=None, category=None,
    is_newsletter=False,
)


def test_summary_serializes_from_alias():
    s = MessageSummary(**base, from_="a@x")
    d = s.model_dump(mode="json")
    assert d["from"] == "a@x" and "from_" not in d and d["account"] == ""


def test_summary_accepts_from_key():
    assert MessageSummary.model_validate({**base, "from": "a@x"}).from_ == "a@x"


def _msg(i):
    return Message(**{**base, "id": f"m{i}"}, from_="a@x", cc="", body="b", attachments=[Attachment(filename="f", mime_type="m", size=1)])


def test_with_account_sets_nested_thread_messages():
    t = Thread(messages=[_msg(1), _msg(2)], replied_by_me=False)
    out = with_account(t, "a")
    assert out.account == "a" and all(m.account == "a" for m in out.messages)
    assert [m.id for m in out.messages] == ["m1", "m2"]
    assert t.account == "" and all(m.account == "" for m in t.messages)


def test_with_account_rejects_models_without_account():
    with pytest.raises(TypeError, match="Label"):
        with_account(Label(id="L", name="n", type="user"), "a")


def test_with_account_simple_and_other_models():
    d = Draft(draft_id="d", message_id="m", thread_id=None)
    assert with_account(d, "a").account == "a"
    ds = DraftSummary(**base, from_="x", draft_id="d")
    assert with_account(ds, "a").model_dump(mode="json")["from"] == "x"
    assert LabelState(id="m", labels=["A"]).labels == ["A"]


def test_message_defaults_and_hidden_removed_not_serialized():
    m = Message(**{**base, "id": "m1"}, from_="a@x", cc="", body="b", attachments=[], hidden_removed=5)
    assert (m.suspicious, m.suspicious_reasons, m.reply_to) == (False, [], "")
    assert m.hidden_removed == 5 and "hidden_removed" not in m.model_dump()
