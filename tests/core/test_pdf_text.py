from mailhub_mcp.core import pdf_text as module
from mailhub_mcp.core.pdf_text import candidate_passwords, pdf_text
from tests.billing_samples import CPF, pdf


def test_plain_pdf_text():
    out = pdf_text(pdf(["Total da fatura R$ 10,00"]), [])
    assert out.error is None and "Total da fatura R$ 10,00" in out.text


def test_encrypted_pdf_opens_with_a_cpf_part():
    data = pdf(["segredo"], password=CPF[:5])
    assert pdf_text(data, []).error == "pdf_locked"
    assert "segredo" in pdf_text(data, candidate_passwords([], [CPF])).text


def test_wrong_passwords_lock():
    assert pdf_text(pdf(["x"], password="outra"), candidate_passwords([], [CPF])).error == "pdf_locked"


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
    got = candidate_passwords(["minha"], ["529.982.247-25"])
    assert got[0] == "minha"
    assert {"529", "5299", "52998", "529982", "529982247", "52998224725", "4725", "24725", "224725",
            "529.982.247-25"} <= set(got)
    assert candidate_passwords([]) == []
    assert candidate_passwords(["a", "a"], ["123"]) == ["a"]


def test_cnpj_variations():
    got = candidate_passwords([], cnpjs=["11.222.333/0001-81"])
    assert got == ["1122", "11222", "112223", "11222333", "112223330001", "11222333000181", "11.222.333/0001-81"]


def test_alphanumeric_cnpj_and_several_documents():
    got = candidate_passwords([], ["52998224725"], ["12.ABC.345/01DE-35", "11222333000181"])
    assert {"12ABC3", "12ABC34501DE35", "12.ABC.345/01DE-35", "112223", "52998"} <= set(got)
    assert candidate_passwords([], cnpjs=["12ABC34501DEXX", "123"]) == []


def test_encrypted_pdf_opens_with_a_cnpj_prefix():
    data = pdf(["empresa"], password="112223")
    assert "empresa" in pdf_text(data, candidate_passwords([], cnpjs=["11222333000181"])).text


def test_load_passwords_reads_every_file(tmp_path):
    from mailhub_mcp.core.billing_read import load_passwords
    (tmp_path / "passwords").write_text("extra\n\n")
    (tmp_path / "cpf").write_text("52998224725\n")
    (tmp_path / "cnpj").write_text("11222333000181\n12.ABC.345/01DE-35\n")
    got = load_passwords(tmp_path)
    assert got[0] == "extra" and {"52998", "112223", "12ABC3"} <= set(got)
    assert load_passwords(tmp_path / "missing") == []
