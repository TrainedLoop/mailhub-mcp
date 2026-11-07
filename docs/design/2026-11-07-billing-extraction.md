# Billing data in read results

Status: implemented
Date: 2026-11-07

## Context

Scheduled routines use `read_emails` to find bills that need paying. Many bills (bank card invoices, utility bills, school fees) carry the amount, due date and payment code only in a PDF attachment, which the model cannot read, so the routine reports them without a value. This design adds a structured `billing` field to read results, filled from the body and, for messages that look like bills, from their PDF attachments.

It deliberately changes one rule of the attachment-links design: the server now reads attachment content. The model still never receives text from an attachment: only numbers, dates and payment codes that pass a validation step.

### Goals

- A bill whose payment code is only in a PDF comes back with its amount, due date and code.
- Nothing free-form from an attachment reaches the model.
- A malicious or broken PDF cannot take down or stall the server.
- Messages that are not bills cost nothing extra.

### Non-goals

- Paying, scheduling or reminding: the field is read-only data.
- OCR of scanned PDFs or images.
- Proving a bill is legitimate. A validated code proves the code is well formed, not that the sender is who it claims.

## Design

### What is extracted

`core/billing.py`, pure code without I/O:

- **Bank slip line (47 digits).** The three field check digits (mod 10) and the general check digit (mod 11 over the rebuilt 44-digit barcode) must all match. From the line: `bank` (3-digit code), `amount` (zero means an open amount and is left out) and `due_date` from the due-date factor (base 1997-10-07, and the factor reset of 2025-02-22, picking the date nearest the message date; factor 0 means none).
- **Collection line (48 digits, first digit 8).** Four blocks of 11 digits plus a check digit (mod 10 or mod 11, chosen by the third digit) and the general check digit. `amount` when the third digit says the value is in reais (6 or 8). No due date.
- **PIX copy-and-paste code (BR Code).** A TLV string starting `000201`, holding `br.gov.bcb.pix`, ending in `6304` plus a valid CRC16-CCITT. From it: `amount` (field 54) and `payee` (field 59).
- **Labelled text.** Only when the message looks like a bill: the first amount after a known label (`total da fatura`, `valor do documento`, `valor a pagar`, `total a pagar`, `valor total`, `valor cobrado`) as `amount_text`, and the first date after `vencimento` as `due_date_text`. Only the parsed number or ISO date is kept, never the surrounding text.

"Looks like a bill" is a keyword check on the subject, body and attachment names (`boleto`, `fatura`, `vencimento`, `vence`, `cobrança`, `pix`, `pagamento`, `segunda via`, `invoice`, `payment due`).

### Reading PDFs

`core/pdf_text.py` runs pypdf in a child process (`python -I -m mailhub_mcp.core.pdf_worker`). The child sets its own limits before parsing (address space 256 MB, CPU 10 s) and the parent kills it after 15 s. Passwords and bytes go through stdin, never the command line. At most 20 pages are read and 200,000 characters returned. Results: the text, or one of `pdf_locked`, `pdf_unreadable`, `pdf_timeout`, `pdf_too_large`.

Passwords tried: none, then each line of `<BILLING_SECRETS_DIR>/passwords`, then parts of the CPF in `<BILLING_SECRETS_DIR>/cpf` (the first 3, 4, 5, 6, 9 and 11 digits, the last 4, 5 and 6, and the formatted CPF), then parts of each CNPJ in `<BILLING_SECRETS_DIR>/cnpj` (the first 4, 5, 6, 8, 12 and 14 characters and the formatted CNPJ; the alphanumeric CNPJ is accepted). All files are optional, hold one entry per line, read at startup, kept out of `repr` and logs.

### Which messages and which files

In `read_email`, `read_emails` and `read_thread`, for each message:

1. Spam, or a suspicion reason that blocks downloads: no `billing`. A bill from a message that looks like an injection or phishing attempt is exactly the one not to hand over as payment data.
2. The full body (before truncation) is scanned for codes.
3. If the message looks like a bill, labelled text is read from the body, and up to 3 PDF attachments of at most 5 MB each are fetched and read, with at most 10 PDFs per tool call. Others get `skipped_limit` or `pdf_too_large`.

Fetching a PDF uses the `attachmentId` from the read that just happened (`MailProvider.attachment_data`, 5 Gmail units), falling back to `get_attachment` when the part had its bytes inline. PDFs of a call are read two at a time.

### Output

```json
"billing": [
  {"kind": "boleto", "source": "fatura.pdf", "line": "2379...", "bank": "237",
   "amount": "1234.56", "due_date": "2026-11-15", "amount_text": "1234.56", "due_date_text": "2026-11-15"},
  {"kind": "pix", "source": "body", "pix_code": "#3", "payee": "#3", "amount": "50.00"}
],
"billing_errors": [{"source": "extrato.pdf", "error": "pdf_locked"}]
```

- `kind` is `boleto`, `arrecadacao`, `pix` or `text` (labelled values with no code).
- `amount` and `due_date` appear only when they come from a validated code; `_text` fields are unverified.
- `pix_code` and `payee` are sender text, so they are references into the response's untrusted fence. Bank slip lines are digits only and stay inline.
- Codes are deduplicated per message. Neither key appears when there is nothing to report.

### Configuration

`BILLING=on|off` (default on) and `BILLING_SECRETS_DIR` (default `/secrets/billing`, mounted read-only by the compose file).

## Testing

- Bank slip, collection and PIX validation with generated valid codes, every corrupted digit, both due-date cycles, open amounts.
- Labelled text in Portuguese formats; nothing but numbers and dates comes out.
- PDF worker: plain PDF, encrypted PDF opened with a CPF part, wrong password, garbage bytes, a page cap.
- Read tools: a bill with the code only in a PDF; suspicious and spam messages get nothing; non-bills fetch no attachment; limits per message and per call; `BILLING=off`.
- Boundaries: only the billing path and the admin download route fetch attachment bytes.
