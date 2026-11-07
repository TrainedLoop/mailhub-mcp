"""No personal deployment details in the public repository."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built by concatenation so this file does not match its own patterns.
FORBIDDEN = [
    re.compile("tail" + r"[0-9a-f]{4,}\.ts\.net", re.IGNORECASE),  # a real tailnet name
    re.compile("purple" + "metrics", re.IGNORECASE),
]
SKIP_FILES = {"uv.lock", Path(__file__).name}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [ROOT / name for name in out.split("\0") if name and Path(name).name not in SKIP_FILES]


def test_no_personal_details_in_tracked_files():
    offenders = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):  # binary (images) or deleted in the working tree
            continue
        offenders += [f"{path.relative_to(ROOT)}: {p.pattern}" for p in FORBIDDEN if p.search(text)]
    assert offenders == []


def test_patterns_catch_examples():
    assert FORBIDDEN[0].search("https://box." + "tail" + "1234ab.ts.net/mcp")
    assert not FORBIDDEN[0].search("https://box.example.ts.net/mcp")
    assert FORBIDDEN[1].search("me@" + "Purple" + "Metrics.com.br")
