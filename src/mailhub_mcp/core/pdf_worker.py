"""Child process that extracts text from one PDF; run only by core.pdf_text.

Reads a JSON header line ({"passwords": [...], "max_pages": n, "max_chars": n})
then the PDF bytes from stdin, and writes one JSON line: {"text": ...} or
{"error": code}. Limits its own memory and CPU before touching the file.
"""

import io
import json
import resource
import sys

MEMORY_BYTES = 256 * 1024 * 1024
CPU_SECONDS = 10


def _limit() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))


def _extract(data: bytes, passwords: list[str], max_pages: int, max_chars: int) -> dict:
    import logging

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    logging.disable(logging.CRITICAL)  # pypdf warns on malformed files; stderr is not read
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not any(reader.decrypt(p) for p in ["", *passwords]):
            return {"error": "pdf_locked"}
        parts: list[str] = []
        size = 0
        for page in reader.pages[:max_pages]:
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text)
            if size >= max_chars:
                break
        return {"text": "\n".join(parts)[:max_chars]}
    except (PdfReadError, ValueError, KeyError, TypeError, IndexError, AttributeError, NotImplementedError):
        return {"error": "pdf_unreadable"}


def main() -> None:
    _limit()
    header = json.loads(sys.stdin.buffer.readline())
    data = sys.stdin.buffer.read()
    try:
        out = _extract(data, header["passwords"], header["max_pages"], header["max_chars"])
    except MemoryError:
        out = {"error": "pdf_too_large"}
    except Exception:  # noqa: BLE001 - any parser failure is an unreadable file, never a crash report
        out = {"error": "pdf_unreadable"}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
