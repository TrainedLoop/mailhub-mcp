"""Billing data of the messages of one read: the body, then the PDFs of messages that look like bills."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from mailhub_mcp.core import billing
from mailhub_mcp.core.errors import MailhubError
from mailhub_mcp.core.models import Attachment, Message
from mailhub_mcp.core.pdf_text import MAX_PDF_BYTES, candidate_passwords, pdf_text

PDFS_PER_MESSAGE = 3
PDFS_PER_CALL = 10
PDF_WORKERS = 2


def load_passwords(secrets_dir: Path) -> list[str]:
    """PDF passwords from `<dir>/passwords`, `<dir>/cpf` and `<dir>/cnpj` (one entry per line); all optional."""
    def read(name: str) -> str:
        try:
            return (secrets_dir / name).read_text(encoding="utf-8")
        except OSError:  # missing or unreadable: run without those passwords
            return ""

    def lines(name: str) -> list[str]:
        return [line.strip() for line in read(name).splitlines() if line.strip()]

    return candidate_passwords(lines("passwords"), lines("cpf"), lines("cnpj"))


def _is_pdf(a: Attachment) -> bool:
    return a.mime_type.lower() == "application/pdf" or a.filename.lower().endswith(".pdf")


def _near(m: Message) -> date:
    if m.internal_date:
        return datetime.fromtimestamp(m.internal_date / 1000, UTC).date()
    return datetime.now(UTC).date()


def collect(provider, messages: list[Message], passwords: list[str]) -> dict[str, billing.BillingResult]:
    """BillingResult per message id, for the given (already vetted) messages; only non-empty results."""
    results: dict[str, billing.BillingResult] = {}
    jobs: list[tuple[Message, Attachment]] = []
    for m in messages:
        result = billing.BillingResult()
        bill = billing.looks_like_bill(m.subject, m.body, *(a.filename for a in m.attachments))
        result.add(billing.extract(m.body, "body", _near(m), labelled=bill))
        if bill:
            for n, a in enumerate(a for a in m.attachments if _is_pdf(a)):
                if n >= PDFS_PER_MESSAGE or len(jobs) >= PDFS_PER_CALL:
                    result.errors.append((a.filename, "skipped_limit"))
                elif a.size > MAX_PDF_BYTES:
                    result.errors.append((a.filename, "pdf_too_large"))
                else:
                    jobs.append((m, a))
        results[m.id] = result

    def run(job: tuple[Message, Attachment]) -> tuple[str, list[billing.BillingItem], str | None]:
        m, a = job
        try:
            data = provider.attachment_data(m.id, a)
        except MailhubError:
            return m.id, [], "fetch_failed"
        try:
            out = pdf_text(data, passwords)
        except Exception:  # noqa: BLE001 - a PDF that cannot be read never makes the email unreadable
            return m.id, [], "pdf_unreadable"
        if out.error:
            return m.id, [], out.error
        return m.id, billing.extract(out.text, a.filename, _near(m), labelled=True), None

    if jobs:
        with ThreadPoolExecutor(PDF_WORKERS) as pool:
            for (_, a), (mid, items, error) in zip(jobs, pool.map(run, jobs), strict=True):
                if error:
                    results[mid].errors.append((a.filename, error))
                results[mid].add(items)
    return {mid: r for mid, r in results.items() if r.items or r.errors}
