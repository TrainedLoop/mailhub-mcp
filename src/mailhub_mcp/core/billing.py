"""Payment data found in a bill: validated payment codes and labelled values.

Pure functions over text. Only numbers, ISO dates and codes that pass their
check digits leave this module; free text never does (the PIX code and payee
are sender text and are fenced by the server).
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

# Due-date factor: days since 1997-10-07, restarted at 1000 on 2025-02-22.
_FACTOR_BASE = date(1997, 10, 7)
_FACTOR_RESTART = date(2025, 2, 22)

_LOOKS_LIKE_BILL = re.compile(
    r"\bboletos?\b|\bfaturas?\b|\bvencimento\b|\bvence\b|\bcobran[çc]a\b|\bpix\b|\bpagamento\b"
    r"|\bsegunda\s+via\b|\b2[ªa]\s+via\b|\binvoice\b|\bpayment\s+due\b",
    re.IGNORECASE,
)
# Runs of digits with the separators a slip line is printed with.
_DIGIT_RUN = re.compile(r"\d[\d .\-]{40,90}\d")
_PIX_START = re.compile(r"000201")
_PIX_END = re.compile(r"6304([0-9A-Fa-f]{4})")
_MAX_PIX_CHARS = 512
_AMOUNT_LABEL = re.compile(
    r"(?:total\s+da\s+fatura|valor\s+do\s+documento|valor\s+a\s+pagar|total\s+a\s+pagar|valor\s+total|valor\s+cobrado)"
    r"[^\d]{0,40}?(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})",
    re.IGNORECASE,
)
_DUE_LABEL = re.compile(r"vencimento[^\d]{0,40}?(\d{2})/(\d{2})/(\d{4}|\d{2})\b", re.IGNORECASE)


@dataclass
class BillingItem:
    kind: str  # boleto | arrecadacao | pix | text
    source: str  # "body" or an attachment name
    line: str | None = None
    bank: str | None = None
    amount: Decimal | None = None
    due_date: date | None = None
    pix_code: str | None = None
    payee: str | None = None
    amount_text: Decimal | None = None
    due_date_text: date | None = None

    def key(self) -> str:
        return self.line or self.pix_code or f"text:{self.amount_text}:{self.due_date_text}"


@dataclass
class BillingResult:
    items: list[BillingItem] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (source, error code)

    def add(self, items: list[BillingItem]) -> None:
        seen = {i.key() for i in self.items}
        for item in items:
            if item.key() not in seen:
                seen.add(item.key())
                self.items.append(item)


def looks_like_bill(*texts: str) -> bool:
    return any(_LOOKS_LIKE_BILL.search(t) for t in texts if t)


# --- check digits ---

def _mod10(digits: str) -> int:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10


def _mod11_weights(digits: str) -> int:
    return sum(int(d) * (2 + i % 8) for i, d in enumerate(reversed(digits)))


def _mod11_boleto(digits: str) -> int:
    dv = 11 - _mod11_weights(digits) % 11
    return 1 if dv in (0, 10, 11) else dv


def _mod11_arrecadacao(digits: str) -> int:
    rest = _mod11_weights(digits) % 11
    return 0 if rest in (0, 1) else 11 - rest


def _due_date(factor: int, near: date) -> date | None:
    if factor == 0:
        return None
    candidates = [_FACTOR_BASE + timedelta(days=factor)]
    if factor >= 1000:
        candidates.append(_FACTOR_RESTART + timedelta(days=factor - 1000))
    return min(candidates, key=lambda d: abs((d - near).days))


def parse_boleto(line: str, near: date) -> BillingItem | None:
    """A 47-digit bank slip line, or None when any check digit fails."""
    if len(line) != 47 or not line.isdigit():
        return None
    fields = ((line[0:9], line[9]), (line[10:20], line[20]), (line[21:31], line[31]))
    if any(_mod10(body) != int(dv) for body, dv in fields):
        return None
    barcode = line[0:4] + line[32] + line[33:47] + line[4:9] + line[10:20] + line[21:31]
    if _mod11_boleto(barcode[:4] + barcode[5:]) != int(barcode[4]):
        return None
    cents = int(line[37:47])
    return BillingItem(
        kind="boleto", source="", line=line, bank=line[0:3],
        amount=Decimal(cents) / 100 if cents else None,
        due_date=_due_date(int(line[33:37]), near),
    )


def parse_arrecadacao(line: str) -> BillingItem | None:
    """A 48-digit collection line (utilities, taxes), or None when any check digit fails."""
    if len(line) != 48 or not line.isdigit() or line[0] != "8" or line[2] not in "6789":
        return None
    check = _mod10 if line[2] in "67" else _mod11_arrecadacao
    blocks = [line[i:i + 11] for i in range(0, 48, 12)]
    if any(check(b) != int(line[i + 11]) for b, i in zip(blocks, range(0, 48, 12), strict=True)):
        return None
    barcode = "".join(blocks)
    if check(barcode[:3] + barcode[4:]) != int(barcode[3]):
        return None
    cents = int(barcode[4:15]) if line[2] in "68" else 0
    return BillingItem(kind="arrecadacao", source="", line=line, amount=Decimal(cents) / 100 if cents else None)


def _crc16(data: str) -> int:
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _tlv(data: str) -> dict[str, str] | None:
    out: dict[str, str] = {}
    i = 0
    while i < len(data):
        if i + 4 > len(data) or not data[i:i + 4].isdigit():
            return None
        tag, size = data[i:i + 2], int(data[i + 2:i + 4])
        value = data[i + 4:i + 4 + size]
        if len(value) != size:
            return None
        out[tag] = value
        i += 4 + size
    return out


def parse_pix(code: str) -> BillingItem | None:
    """A PIX copy-and-paste code (BR Code), or None when its CRC or structure is wrong."""
    if not code.startswith("000201") or len(code) > _MAX_PIX_CHARS or code[-8:-4] != "6304":
        return None
    if f"{_crc16(code[:-4]):04X}" != code[-4:].upper():
        return None
    fields = _tlv(code)
    if fields is None or "br.gov.bcb.pix" not in fields.get("26", "").lower():
        return None
    amount = None
    if re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", fields.get("54", "")):
        amount = Decimal(fields["54"])
    return BillingItem(kind="pix", source="", pix_code=code, payee=fields.get("59") or None, amount=amount)


# --- finding codes in text ---

def _slip_lines(text: str, near: date) -> list[BillingItem]:
    found: list[BillingItem] = []
    for run in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", run.group())
        for size, parse in ((48, lambda s: parse_arrecadacao(s)), (47, lambda s: parse_boleto(s, near))):
            for start in range(len(digits) - size + 1):
                item = parse(digits[start:start + size])
                if item is not None:
                    found.append(item)
                    break
            else:
                continue
            break
    return found


def _pix_codes(text: str) -> list[BillingItem]:
    found: list[BillingItem] = []
    for candidate in (text, re.sub(r"[\r\n]+", "", text)):
        for start in _PIX_START.finditer(candidate):
            window = candidate[start.start():start.start() + _MAX_PIX_CHARS]
            for end in _PIX_END.finditer(window):
                item = parse_pix(window[:end.end()])
                if item is not None:
                    found.append(item)
                    break
    return found


def _decimal_br(raw: str) -> Decimal:
    return Decimal(raw.replace(".", "").replace(",", "."))


def _labelled(text: str) -> BillingItem | None:
    amount = due = None
    if m := _AMOUNT_LABEL.search(text):
        amount = _decimal_br(m.group(1))
    if m := _DUE_LABEL.search(text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            due = date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            due = None
    if amount is None and due is None:
        return None
    return BillingItem(kind="text", source="", amount_text=amount, due_date_text=due)


def extract(text: str, source: str, near: date, labelled: bool) -> list[BillingItem]:
    """Codes in `text` and, when `labelled`, the labelled amount and due date.

    The labelled values go onto the first code found, or into a "text" item when there is none.
    """
    items = _slip_lines(text, near) + _pix_codes(text)
    label = _labelled(text) if labelled else None
    if label is not None:
        if items:
            items[0].amount_text, items[0].due_date_text = label.amount_text, label.due_date_text
        else:
            items.append(label)
    for item in items:
        item.source = source
    return items
