import pytest

from mailhub_mcp.core.drafts import DraftRefused, resolve_recipients, thread_participants
from mailhub_mcp.core.models import Message, Thread


def msg(id, from_="", to="", cc="", reply_to=""):
    return Message(
        id=id, thread_id="t", from_=from_, to=to, subject="s", date="", snippet="", labels=[],
        internal_date=0, unread=False, starred=False, is_important=None, category=None,
        is_newsletter=False, reply_to=reply_to, cc=cc, body="", attachments=[],
    )


@pytest.fixture
def thread():
    return Thread(
        messages=[
            msg("m1", from_="Bob <Bob@Example.com>", to="me@x.com, Carol <carol@example.com>", reply_to="List <list@example.com>"),
            msg("m2", from_="me@x.com", to="bob@example.com", cc="dave@example.com"),
        ],
        replied_by_me=True,
    )


def test_participants_normalized_and_include_own(thread):
    assert thread_participants(thread, "Me@X.com") == {
        "bob@example.com", "me@x.com", "carol@example.com", "list@example.com", "dave@example.com",
    }


def test_default_recipient_is_reply_to_else_from(thread):
    assert resolve_recipients(thread, "m1", "me@x.com", None, None) == ("list@example.com", None)
    assert resolve_recipients(thread, "m2", "me@x.com", None, None) == ("me@x.com", None)


def test_allowed_variants(thread):
    to, cc = resolve_recipients(thread, "m1", "me@x.com", "BOB@example.COM, Someone <carol@example.com>", "Dave <dave@example.com>")
    assert to == "bob@example.com, carol@example.com" and cc == "dave@example.com"


def test_outsider_refused_and_allowed_listed(thread):
    with pytest.raises(DraftRefused, match="evil@attacker.com") as e:
        resolve_recipients(thread, "m1", "me@x.com", "bob@example.com, evil@attacker.com", None)
    assert "carol@example.com" in str(e.value) and "dave@example.com" in str(e.value)


def test_cc_outsider_refused(thread):
    with pytest.raises(DraftRefused, match="evil@attacker.com"):
        resolve_recipients(thread, "m1", "me@x.com", None, "evil@attacker.com")


def test_unknown_message_in_thread(thread):
    with pytest.raises(DraftRefused, match="nope"):
        resolve_recipients(thread, "nope", "me@x.com", None, None)
