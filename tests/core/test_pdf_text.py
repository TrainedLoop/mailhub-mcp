from mailhub_mcp.core import pdf_text as module
from mailhub_mcp.core.pdf_text import candidate_passwords, pdf_text
from tests.billing_samples import CPF, pdf


def test_plain_pdf_text():
    out = pdf_text(pdf(["Total da fatura R$ 10,00"]), [])
    assert out.error is None and "Total da fatura R$ 10,00" in out.text


def test_encrypted_pdf_opens_with_a_cpf_part():
    data = pdf(["segredo"], password=CPF[:5])
    assert pdf_text(data, []).error == "pdf_locked"
    assert "segredo" in pdf_text(data, candidate_passwords(CPF, [])).text


def test_wrong_passwords_lock():
    assert pdf_text(pdf(["x"], password="outra"), candidate_passwords(CPF, [])).error == "pdf_locked"


def test_garbage_is_unreadable():
    assert pdf_text(b"%PDF-1.4 not really", []).error == "pdf_unreadable"
    assert pdf_text(b"\x00" * 100, []).error == "pdf_unreadable"


def test_too_large_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(module, "MAX_PDF_BYTES", 10)
    assert pdf_text(pdf(["x"]), []).error == "pdf_too_large"


def test_timeout(monkeypatch):
    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.001)
    assert pdf_text(pdf(["x"]), []).error == "pdf_timeout"


def test_candidate_passwords():
    got = candidate_passwords("529.982.247-25", ["minha"])
    assert got[0] == "minha"
    assert {"529", "5299", "52998", "529982", "529982247", "52998224725", "4725", "24725", "224725",
            "529.982.247-25"} <= set(got)
    assert candidate_passwords("", []) == []
    assert candidate_passwords("123", ["a", "a"]) == ["a"]
