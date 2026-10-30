import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "src" / "mailhub_mcp" / "core"


def test_core_has_no_provider_google_or_fastmcp_imports():
    pattern = re.compile(
        r"^\s*(from|import)\s+(mailhub_mcp\.providers|google|googleapiclient|fastmcp)\b", re.M
    )
    files = list(CORE.rglob("*.py"))
    assert files
    offenders = [p for p in files if pattern.search(p.read_text())]
    assert offenders == []
