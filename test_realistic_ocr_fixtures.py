from __future__ import annotations

from pathlib import Path

from invoice_parser_helpers import build_invoice_json, extract_invoice_number


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr_texts"


def parse_fixture(name: str) -> dict:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return build_invoice_json(
        ocr_text=text,
        pdf_text=text,
        source_type="fixture",
        original_filename=name,
        mime_type="text/plain",
        sha256=name.encode("utf-8").hex().ljust(64, "0")[:64],
        ocr_engine="fixture_text",
    )


def fields(invoice: dict) -> dict:
    return invoice["extraccion_enriquecida"]["campos"]


def test_realistic_invoice_with_iibb_is_ok_and_keeps_legacy_shape() -> None:
    invoice = parse_fixture("proveedor_con_iibb_ok.txt")
    enriched = fields(invoice)

    assert invoice["estado"] == "OK"
    assert invoice["comprobante"]["letra"] == "A"
    assert invoice["comprobante"]["punto_venta"] == "0003"
    assert invoice["comprobante"]["numero"] == "00000042"
    assert invoice["comprobante"]["fecha_emision"] == "2026-06-12"
    assert invoice["emisor"]["cuit"] == "30-50000007-0"
    assert invoice["importes"]["total"] == 1245.0
    assert invoice["importes"]["iva_21"] == 210.0
    assert invoice["importes"]["percepciones_iibb"] == 30.0
    assert enriched["proveedor_cuit"]["valor"] == "30-50000007-0"
    assert enriched["tipo_comprobante"]["valor"] == "FACTURA_A"
    assert enriched["codjur"]["valor"] == "914"
    assert enriched["percepciones_iibb"]["valor"] == "30.00"
    assert "comprobante" in invoice and "importes" in invoice and "validaciones" in invoice


def test_issuer_cuit_wins_when_receiver_cuit_appears_first() -> None:
    invoice = parse_fixture("receptor_cuit_antes_del_emisor.txt")
    enriched = fields(invoice)

    assert invoice["estado"] == "OK"
    assert invoice["emisor"]["cuit"] == "30-71471099-7"
    assert enriched["proveedor_cuit"]["valor"] == "30-71471099-7"
    assert invoice["receptor"]["cuit"] != invoice["emisor"]["cuit"]


def test_phone_numbers_are_not_used_as_invoice_number() -> None:
    text = (FIXTURE_DIR / "telefono_no_es_comprobante.txt").read_text(encoding="utf-8")
    number = extract_invoice_number(text)
    invoice = parse_fixture("telefono_no_es_comprobante.txt")

    assert number["punto_venta"] == "0007"
    assert number["numero"] == "00000128"
    assert invoice["estado"] == "OK"


def test_subtotal_and_total_iva_are_not_taken_as_final_total() -> None:
    invoice = parse_fixture("subtotal_y_total_iva_no_son_total.txt")
    enriched = fields(invoice)

    assert invoice["estado"] == "OK"
    assert invoice["importes"]["total"] == 1210.0
    assert enriched["neto_gravado"]["valor"] == "1000.00"
    assert enriched["total"]["valor"] == "1210.00"
    assert "Total a pagar" in enriched["total"]["evidencia"]


def test_iva_percentage_without_amount_does_not_become_zero_amount() -> None:
    invoice = parse_fixture("iva_porcentaje_sin_importe_review.txt")
    enriched = fields(invoice)

    assert invoice["estado"] == "REVIEW_REQUIRED"
    assert enriched["iva_21"]["valor"] is None
    assert any(failure["codigo"] == "TOTAL_MISMATCH" for failure in invoice["extraccion_enriquecida"]["validaciones"]["fallas"])
