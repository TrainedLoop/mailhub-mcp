"""Text of a PDF, extracted in a resource-limited child process."""

import json
import subprocess
import sys
from dataclasses import dataclass

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 20
MAX_CHARS = 200_000
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class PdfText:
    text: str = ""
    error: str | None = None  # pdf_locked | pdf_unreadable | pdf_timeout | pdf_too_large


def pdf_text(data: bytes, passwords: list[str]) -> PdfText:
    if len(data) > MAX_PDF_BYTES:
        return PdfText(error="pdf_too_large")
    header = json.dumps({"passwords": passwords, "max_pages": MAX_PAGES, "max_chars": MAX_CHARS})
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "mailhub_mcp.core.pdf_worker"],
            input=header.encode() + b"\n" + data,
            capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return PdfText(error="pdf_timeout")
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        # Killed by its CPU or memory limit before answering.
        return PdfText(error="pdf_unreadable")
    if not isinstance(out, dict):
        return PdfText(error="pdf_unreadable")
    if isinstance(out.get("text"), str):
        return PdfText(text=out["text"][:MAX_CHARS])
    error = out.get("error")
    return PdfText(error=error if error in ("pdf_locked", "pdf_unreadable", "pdf_too_large") else "pdf_unreadable")


def candidate_passwords(cpf: str, extra: list[str]) -> list[str]:
    """Passwords to try: the extra ones first, then the usual bank variations of the CPF."""
    out = list(extra)
    digits = "".join(c for c in cpf if c.isdigit())
    if len(digits) == 11:
        out += [digits[:n] for n in (3, 4, 5, 6, 9, 11)]
        out += [digits[-n:] for n in (4, 5, 6)]
        out.append(f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}")
    return list(dict.fromkeys(p for p in out if p))
