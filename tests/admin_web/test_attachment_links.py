import re
import unicodedata
from urllib.parse import unquote

import pytest

from mailhub_mcp.admin_web.attachment_links import (
    FILES_PATH,
    LINK_SECONDS,
    AttachmentLinks,
    LinkTarget,
    content_disposition,
    derive_link_key,
)
from mailhub_mcp.admin_web.session import Signer, derive_key
from mailhub_mcp.core.attachments import GMAIL_BLOCKED_EXTENSIONS, download_block_reason

SECRET = "test-signing-secret"
NOW = 1_800_000_000.0


def make(now: float = NOW) -> AttachmentLinks:
    return AttachmentLinks(derive_link_key(SECRET), "https://example.test/", lambda: now)


def test_round_trip_and_url():
    links = make()
    token = links.token("box", "m1", "2")
    assert links.decode(token) == LinkTarget("box", "m1", "2")
    url = links.url("box", "m1", "2")
    assert url.startswith("https://example.test/admin/files/")
    assert url == f"https://example.test{FILES_PATH}{token}"


def test_token_is_url_safe_for_admin_path():
    token = make().token("box", "m1", "2")
    assert re.fullmatch(r"[A-Za-z0-9_.\-]+", token)


def test_expiry_boundary():
    token = make().token("box", "m1", "2")
    assert make(NOW + LINK_SECONDS - 1).decode(token) == LinkTarget("box", "m1", "2")
    assert make(NOW + LINK_SECONDS).decode(token) == "expired"


def _flip(ch: str) -> str:
    return "B" if ch != "B" else "C"


def test_tampering_returns_none():
    links = make()
    token = links.token("box", "m1", "2")
    payload, sig = token.split(".")
    assert links.decode(f"{_flip(payload[3])}".join([payload[:3], payload[4:]]) + "." + sig) is None
    assert links.decode(payload + "." + sig[:3] + _flip(sig[3]) + sig[4:]) is None


def test_session_key_token_rejected():
    session_signer = Signer(derive_key(SECRET))
    token = session_signer.dumps({"a": "box", "m": "m1", "p": "2", "exp": 9e9})
    assert make().decode(token) is None


@pytest.mark.parametrize("bad", ["", "é.é", "a.b.c", "nodot", "...."])
def test_malformed_tokens(bad):
    assert make().decode(bad) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"a": "x", "m": "y", "p": 2, "exp": 9e9},
        {"a": "x", "m": "y", "p": "2", "exp": "9e9"},
        {"a": "x", "m": "y", "p": "2", "exp": True},
        {"a": "x", "m": "y", "exp": 9e9},
        {"a": "x", "m": "y", "p": "2", "exp": 9e9, "extra": 1},
        {"a": "x", "m": "y", "p": "2", "exp": float("nan")},
        {"a": "x", "m": "y", "p": "2", "exp": float("inf")},
    ],
)
def test_wrong_shape_under_valid_signature(payload):
    token = Signer(derive_link_key(SECRET)).dumps(payload)
    assert make().decode(token) is None


def test_link_key_differs_from_session_key():
    assert derive_link_key(SECRET) != derive_key(SECRET)
    assert len(derive_link_key(SECRET)) == 32


def test_content_disposition_non_ascii():
    assert content_disposition("fatura março.pdf") == (
        "attachment; filename=\"fatura marco.pdf\"; "
        "filename*=UTF-8''fatura%20mar%C3%A7o.pdf"
    )


def test_content_disposition_hostile_name():
    header = content_disposition('../"x"\r\n.pdf')
    quoted = re.search(r'filename="([^"]*)"', header).group(1)
    assert "/" not in header and "\r" not in header and "\n" not in header
    assert quoted == "x.pdf"
    assert '"' not in quoted


def test_content_disposition_semicolon_and_apostrophe():
    header = content_disposition("a;b'c.pdf")
    assert header == "attachment; filename=\"a_b_c.pdf\"; filename*=UTF-8''a%3Bb%27c.pdf"


@pytest.mark.parametrize("name", ["...", '"\x00'])
def test_content_disposition_empty_after_sanitizing(name):
    assert 'filename="attachment"' in content_disposition(name)


def test_content_disposition_truncates():
    header = content_disposition("a" * 300)
    star = header.split("filename*=UTF-8''")[1]
    assert len(unquote(star)) == 150


def test_content_disposition_truncation_keeps_last_extension():
    header = content_disposition("A" * 146 + ".exe" + "B" * 20 + ".pdf")
    assert header.split("filename*=UTF-8''")[1] == "A" * 146 + ".pdf"


def _saved_names(header: str) -> list[str]:
    """The names a browser could save: the filename* value and the ASCII fallback."""
    quoted = re.search(r'filename="([^"]*)"', header).group(1)
    return [unquote(header.split("filename*=UTF-8''")[1]), quoted]


def _saved_extension(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    while name and (name[-1] == "." or name[-1].isspace() or unicodedata.category(name[-1])[0] in "CZ"):
        name = name[:-1]
    return name.rpartition(".")[2].lower() if "." in name else ""


@pytest.mark.parametrize("name", [
    "invoice.exe\u00a0",
    "invoice.exe\u3000",
    "invoice.exe\u2003",
    "invoice.exe\u00a0.",
    "A" * 146 + ".exe" + "B" * 20 + ".pdf",
    "a.\uff45\uff58\uff45",
    "a\u2024exe",
    "a.exe\u4e2d",
    "a.ex\u00e9",
    "invoice.pdf",
    "fatura março.pdf",
])
def test_served_names_never_end_in_a_blocked_type_unless_blocked(name):
    if download_block_reason(False, False, name) == "blocked_type":
        return
    for saved in _saved_names(content_disposition(name)):
        assert _saved_extension(saved) not in GMAIL_BLOCKED_EXTENSIONS, saved
