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


def _cpf_variations(cpf: str) -> list[str]:
    digits = "".join(c for c in cpf if c.isdigit())
    if len(digits) != 11:
        return []
    return [
        *(digits[:n] for n in (3, 4, 5, 6, 9, 11)),
        *(digits[-n:] for n in (4, 5, 6)),
        f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}",
    ]


def _cnpj_variations(cnpj: str) -> list[str]:
    # 14 characters; the first 12 may be letters in the alphanumeric CNPJ issued from 2026.
    chars = "".join(c for c in cnpj.upper() if c.isalnum())
    if len(chars) != 14 or not chars[12:].isdigit():
        return []
    return [
        *(chars[:n] for n in (4, 5, 6, 8, 12, 14)),
        f"{chars[:2]}.{chars[2:5]}.{chars[5:8]}/{chars[8:12]}-{chars[12:]}",
    ]


def candidate_passwords(extra: list[str], cpfs: list[str] = (), cnpjs: list[str] = ()) -> list[str]:
    """Passwords to try: the extra ones first, then the usual bank variations of each CPF and CNPJ."""
    out = list(extra)
    for cpf in cpfs:
        out += _cpf_variations(cpf)
    for cnpj in cnpjs:
        out += _cnpj_variations(cnpj)
    return list(dict.fromkeys(p for p in out if p))
