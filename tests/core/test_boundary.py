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


PROVIDERS = CORE.parent / "providers"


def test_providers_import_only_models_errors_content_from_core():
    pattern = re.compile(r"^\s*(?:from|import)\s+mailhub_mcp\.core(?:\.(\w+))?", re.M)
    files = list(PROVIDERS.rglob("*.py"))
    assert files
    offenders = {
        str(p.relative_to(PROVIDERS)): m.group(0).strip()
        for p in files for m in pattern.finditer(p.read_text())
        if m.group(1) not in ("models", "errors", "content")
    }
    assert offenders == {}
