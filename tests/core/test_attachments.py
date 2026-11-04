import pytest

from mailhub_mcp.core.attachments import GMAIL_BLOCKED_EXTENSIONS, download_block_reason
from mailhub_mcp.core.models import Attachment, Message


@pytest.mark.parametrize("spam,suspicious,name,expected", [
    (True, True, "a.exe", "spam"),
    (False, True, "a.exe", "suspicious"),
    (False, False, "a.exe", "blocked_type"),
    (False, False, "INVOICE.PDF.EXE", "blocked_type"),
    (False, False, "invoice.exe.", "blocked_type"),
    (False, False, "invoice.exe ", "blocked_type"),
    (False, False, "invoice.pdf", None),
    (False, False, "archive.zip", None),
    (False, False, "page.html", None),
    (False, False, "noextension", None),
])
def test_download_block_reason(spam, suspicious, name, expected):
    assert download_block_reason(spam, suspicious, name) == expected


def test_every_listed_extension_is_blocked():
    assert {"exe", "js", "bat", "scr", "vbs", "jar", "ex_", "xll"} <= GMAIL_BLOCKED_EXTENSIONS
    for ext in GMAIL_BLOCKED_EXTENSIONS:
        assert download_block_reason(False, False, f"file.{ext}") == "blocked_type"


def test_internal_fields_are_never_serialized():
    m = Message(
        id="m1", thread_id="t", from_="a@example.com", to="b@example.com", subject="s", date="",
        snippet="", labels=[], internal_date=0, unread=False, starred=False, is_important=None,
        category=None, is_newsletter=False, cc="", body="",
        attachments=[Attachment(filename="a.pdf", mime_type="application/pdf", size=1, part_id="1.2")],
        spam=True,
    )
    dumped = m.model_dump()
    assert "spam" not in dumped
    assert "part_id" not in dumped["attachments"][0]
    assert m.spam is True and m.attachments[0].part_id == "1.2"
