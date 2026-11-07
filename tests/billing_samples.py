"""Builders of valid payment codes and small PDFs for the billing tests."""

import io
from datetime import date

from mailhub_mcp.core.billing import _crc16, _mod10, _mod11_arrecadacao, _mod11_boleto

CPF = "52998224725"  # a well-formed test CPF, not a real person's


def boleto(bank="341", cents=123456, factor=None, due=None, free="1234567890123456789012345") -> str:
    """A valid 47-digit slip line."""
    if factor is None:
        factor = 1000 + (due - date(2025, 2, 22)).days if due else 0
    tail = f"{factor:04d}{cents:010d}"
    head = bank + "9"
    dv = _mod11_boleto(head + tail + free)
    f1 = head + free[:5]
    f2 = free[5:15]
    f3 = free[15:25]
    return f"{f1}{_mod10(f1)}{f2}{_mod10(f2)}{f3}{_mod10(f3)}{dv}{tail}"


def arrecadacao(cents=9876, value_type="6", rest="00012345678901234567890123456") -> str:
    """A valid 48-digit collection line (segment 2 = sanitation)."""
    body = "82" + value_type + f"{cents:011d}" + rest[:29]  # 43 digits without the general DV
    check = _mod10 if value_type in "67" else _mod11_arrecadacao
    barcode = body[:3] + str(check(body)) + body[3:]
    blocks = [barcode[i:i + 11] for i in range(0, 44, 11)]
    return "".join(b + str(check(b)) for b in blocks)


def pix(amount="50.00", name="LOJA EXEMPLO", key="pix@example.com") -> str:
    def f(tag, value):
        return f"{tag}{len(value):02d}{value}"

    gui = f("00", "br.gov.bcb.pix") + f("01", key)
    body = f("00", "01") + f("26", gui) + f("52", "0000") + f("53", "986")
    if amount:
        body += f("54", amount)
    body += f("58", "BR") + f("59", name) + f("60", "SAO PAULO") + f("62", f("05", "***")) + "6304"
    return body + f"{_crc16(body):04X}"


def pdf(text_lines: list[str], password: str | None = None) -> bytes:
    """A one-page PDF showing `text_lines`, optionally encrypted with `password`."""
    from pypdf import PdfReader, PdfWriter

    ops = ["BT", "/F1 10 Tf", "14 TL", "40 800 Td"]
    for line in text_lines:
        ops.append("(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
         b" /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    data = out.getvalue()
    if password is None:
        return data
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(password, algorithm="AES-256")
    enc = io.BytesIO()
    writer.write(enc)
    return enc.getvalue()
