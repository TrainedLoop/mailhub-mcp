from datetime import date
from decimal import Decimal

import pytest

from mailhub_mcp.core.billing import (
    extract,
    looks_like_bill,
    parse_arrecadacao,
    parse_boleto,
    parse_pix,
)
from tests.billing_samples import arrecadacao, boleto, pix

NEAR = date(2026, 11, 1)
# Febraban's published example line (Banco do Brasil, R$ 1,00, due 2007-12-31).
FEBRABAN = "00190500954014481606906809350314337370000000100"
# The BCB's published static BR Code example.
BCB_PIX = (
    "00020126580014br.gov.bcb.pix0136123e4567-e12b-12d1-a456-4266554400005204000053039865802BR"
    "5913Fulano de Tal6008BRASILIA62070503***63041D3D"
)


def test_published_examples():
    item = parse_boleto(FEBRABAN, date(2008, 1, 1))
    assert (item.bank, item.amount, item.due_date) == ("001", Decimal("1.00"), date(2007, 12, 31))
    item = parse_pix(BCB_PIX)
    assert (item.payee, item.amount) == ("Fulano de Tal", None)


def test_boleto_amount_and_due_date_after_the_factor_restart():
    item = parse_boleto(boleto(cents=123456, due=date(2026, 11, 15)), NEAR)
    assert (item.kind, item.bank, item.amount, item.due_date) == ("boleto", "341", Decimal("1234.56"), date(2026, 11, 15))


def test_boleto_open_amount_and_no_due_date():
    item = parse_boleto(boleto(cents=0, factor=0), NEAR)
    assert item.amount is None and item.due_date is None


@pytest.mark.parametrize("pos", range(47))
def test_boleto_any_changed_digit_is_rejected(pos):
    line = boleto(due=date(2026, 11, 15))
    changed = line[:pos] + str((int(line[pos]) + 1) % 10) + line[pos + 1:]
    assert parse_boleto(changed, NEAR) is None


@pytest.mark.parametrize("value_type,amount", [("6", Decimal("98.76")), ("7", None), ("8", Decimal("98.76")), ("9", None)])
def test_arrecadacao(value_type, amount):
    item = parse_arrecadacao(arrecadacao(cents=9876, value_type=value_type))
    assert item.kind == "arrecadacao" and item.amount == amount and item.due_date is None


@pytest.mark.parametrize("pos", range(48))
def test_arrecadacao_any_changed_digit_is_rejected(pos):
    line = arrecadacao()
    changed = line[:pos] + str((int(line[pos]) + 1) % 10) + line[pos + 1:]
    assert parse_arrecadacao(changed) is None


def test_pix_amount_and_payee():
    item = parse_pix(pix(amount="50.00", name="LOJA EXEMPLO"))
    assert (item.amount, item.payee) == (Decimal("50.00"), "LOJA EXEMPLO")


@pytest.mark.parametrize("code", [
    pix()[:-1] + ("0" if pix()[-1] != "0" else "1"),  # wrong CRC
    pix().replace("br.gov.bcb.pix", "br.gov.bcb.pyx"),  # not a PIX code (CRC also breaks)
    "000201" + "x" * 600 + "6304ABCD",
])
def test_pix_rejected(code):
    assert parse_pix(code) is None


def test_extract_finds_printed_codes_in_text():
    line = boleto(due=date(2026, 11, 15))
    printed = f"{line[:5]}.{line[5:10]} {line[10:15]}.{line[15:21]} {line[21:26]}.{line[26:32]} {line[32]} {line[33:]}"
    code = pix()
    text = f"Pague com o código:\n{printed}\nou PIX copia e cola:\n{code[:40]}\n{code[40:]}\n"
    items = extract(text, "fatura.pdf", NEAR, labelled=False)
    assert [(i.kind, i.source) for i in items] == [("boleto", "fatura.pdf"), ("pix", "fatura.pdf")]
    assert items[0].line == line and items[1].pix_code == code


def test_extract_ignores_numbers_that_fail_check_digits():
    assert extract("Pedido 12345678901234567890123456789012345678901234567 e CPF 529.982.247-25", "body", NEAR, True) == []


def test_labelled_values_go_onto_the_first_code():
    text = f"Total da fatura: R$ 2.345,67\nVencimento 10/12/2026\n{boleto(cents=0, due=date(2026, 12, 10))}"
    [item] = extract(text, "body", NEAR, labelled=True)
    assert item.amount is None and item.due_date == date(2026, 12, 10)
    assert (item.amount_text, item.due_date_text) == (Decimal("2345.67"), date(2026, 12, 10))


def test_labelled_values_without_a_code():
    [item] = extract("Valor a pagar R$ 89,90 — vencimento: 05/12/26", "body", NEAR, labelled=True)
    assert (item.kind, item.amount_text, item.due_date_text) == ("text", Decimal("89.90"), date(2026, 12, 5))


def test_labelled_values_only_when_asked():
    assert extract("Total da fatura R$ 10,00", "body", NEAR, labelled=False) == []


def test_impossible_labelled_date_is_dropped():
    [item] = extract("Total a pagar R$ 1,00 vencimento 31/02/2026", "body", NEAR, labelled=True)
    assert item.due_date_text is None


@pytest.mark.parametrize("text,expected", [
    ("Sua fatura de novembro", True),
    ("Boleto disponível", True),
    ("Cobrança referente a outubro", True),
    ("2ª via da conta", True),
    ("Invoice #123", True),
    ("Newsletter da semana", False),
    ("Faturamento recorde", False),
])
def test_looks_like_bill(text, expected):
    assert looks_like_bill(text) is expected
