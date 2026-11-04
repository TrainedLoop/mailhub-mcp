import unicodedata

import pytest

from mailhub_mcp.core.attachments import (
    GMAIL_BLOCKED_EXTENSIONS,
    ascii_filename,
    download_block_reason,
    served_filename,
)
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


# Names whose checked extension once differed from the one a browser would save.
BYPASS_NAMES = [
    "invoice.exe\u00a0",
    "invoice.exe\u3000",
    "invoice.exe\u2003",
    "invoice.exe\u2028",
    "invoice.exe\u00a0.",
    "invoice.exe \u00a0 . .",
    "invoice.exe\u200b",
    "A" * 146 + ".exe" + "\u00a0" * 20,
    "a.\uff45\uff58\uff45",
    "a\u2024exe",
    "a.EXE\u2024",
    "a.exe\u4e2d",
    "a.ex\u00e9",
]


@pytest.mark.parametrize("name", BYPASS_NAMES)
def test_bypass_names_are_blocked(name):
    assert download_block_reason(False, False, name) == "blocked_type"


def _browser_trim(name: str) -> str:
    """What a browser may drop from the end of a saved file name."""
    return name.rstrip("".join(c for c in set(name) if c == "." or c.isspace() or unicodedata.category(c)[0] in "CZ"))


def _saved_extension(name: str) -> str:
    trimmed = _browser_trim(unicodedata.normalize("NFKC", name))
    return trimmed.rpartition(".")[2].lower() if "." in trimmed else ""


# Served under a different, harmless name: the truncation one is cut to "AAAA….pdf".
HARMLESS_NAMES = ["A" * 146 + ".exe" + "B" * 20 + ".pdf", "invoice.pdf", "fatura março.pdf", "A" * 300 + ".pdf", "a.exe.pdf", "x" * 200, "..."]


@pytest.mark.parametrize("name", BYPASS_NAMES + HARMLESS_NAMES)
def test_no_servable_name_ends_in_a_blocked_type_unless_blocked(name):
    served = served_filename(name)
    names = [served, ascii_filename(served)]
    if download_block_reason(False, False, name) != "blocked_type":
        assert all(_saved_extension(n) not in GMAIL_BLOCKED_EXTENSIONS for n in names), names


def test_served_filename_truncation_keeps_last_extension():
    served = served_filename("A" * 146 + ".exe" + "B" * 20 + ".pdf")
    assert served == "A" * 146 + ".pdf"
    assert served_filename("a" * 300 + ".pdf") == "a" * 146 + ".pdf"
    assert len(served_filename("a" * 300)) == 150


@pytest.mark.parametrize("name,expected", [
    ("invoice.pdf\u00a0", "invoice.pdf"),
    ("invoice.pdf. .", "invoice.pdf"),
    ("a.\uff50\uff44\uff46", "a.pdf"),
    ("../x.pdf", "x.pdf"),
    ("\uff0e\uff0e\uff0fx.pdf", "x.pdf"),
    ("...", "attachment"),
    ("", "attachment"),
])
def test_served_filename(name, expected):
    assert served_filename(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("fatura março.pdf", "fatura marco.pdf"),
    ("a\u2024exe", "a.exe"),
    ("a.pdf \u4e2d", "a.pdf"),
    ("\u4e2d.pdf", "pdf"),
    ("\u4e2d", "attachment"),
    ("a;b'c.pdf", "a_b_c.pdf"),
])
def test_ascii_filename(name, expected):
    assert ascii_filename(name) == expected


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
