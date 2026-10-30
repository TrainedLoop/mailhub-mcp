import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mailhub_mcp"
PATTERN = re.compile(
    r"\.(trash|untrash|delete|batchDelete|send|filters|forwardingAddresses|sendAs|import_)\("
    r"|messages\(\)\s*\.\s*insert\("
    r"|\.labels\(\)\s*\.(patch|update|delete)\("
)


def test_no_destructive_gmail_calls():
    files = list(SRC.rglob("*.py"))
    assert files
    offenders = [p for p in files if PATTERN.search(p.read_text())]
    assert offenders == []


def test_pattern_catches_forbidden_calls():
    for text in (
        "svc.users().settings().filters().list()",
        "svc.users().settings().forwardingAddresses()",
        "svc.users().settings().sendAs()",
        "svc.users().messages().insert(x)",
        "svc.users().messages().import_(x)",
        "svc.users().labels().patch(x)",
        "svc.users().labels().delete(x)",
        "svc.users().messages().send(x)",
    ):
        assert PATTERN.search(text), text


def test_pattern_ignores_list_insert():
    for text in ("items.insert(0, x)", "self.parts.insert(1, y)"):
        assert not PATTERN.search(text), text
