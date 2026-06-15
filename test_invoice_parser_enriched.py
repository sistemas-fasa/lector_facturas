from __future__ import annotations

from decimal import Decimal
import sys
import types

sys.modules.setdefault("cgi", types.SimpleNamespace(FieldStorage=object))
import host_invoice_parser_service as service
from invoice_parser_helpers import build_invoice_json, sha256_bytes


SAMPLE_TEXT = """ACME SRL
CUIT: 30-50000007-0
FACTURA A
Punto de Venta: 0003 Comp. Nro: 00000042
Fecha de Emision: 12/06/2026
Importe Neto Gravado: 1000,00
IVA 21% 210,00
Percepciones IIBB Misiones 30,00
Otros impuestos: 5,00
Total: $ 1245,00
CAE: 12345678901234
Fecha Vto. CAE: 22/06/2026
"""


def test_build_invoice_json_adds_auditable_enriched_fields() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
        qr_afip={},
    )

    assert invoice["estado"] == "OK"
    enriched = invoice["extraccion_enriquecida"]
    assert enriched["status"] == "OK"
    assert enriched["campos"]["proveedor_cuit"] == {
        "valor": "30-50000007-0",
        "confianza": 85,
        "fuente": "pdf_text",
        "metodo": "regex_cuit",
        "evidencia": "CUIT: 30-50000007-0",
    }
    assert enriched["campos"]["total"]["valor"] == "1245.00"
    assert enriched["campos"]["total"]["fuente"] == "pdf_text"
    assert enriched["validaciones"]["balance_importes"]["ok"] is True
    assert enriched["legacy"]["comprobante"]["punto_venta"] == "0003"


def test_qr_values_have_priority_and_mismatch_is_reported() -> None:
    qr_afip = {
        "detectado": True,
        "url": "https://www.afip.gob.ar/fe/qr/?p=abc",
        "datos": {
            "ver": 1,
            "fecha": "2026-06-13",
            "cuit": 30500000070,
            "ptoVta": 4,
            "tipoCmp": 1,
            "nroCmp": 99,
            "importe": 1300.50,
            "moneda": "PES",
            "codAut": 99998888777766,
        },
    }

    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="b" * 64,
        ocr_engine="ocr",
        pdf_text="",
        qr_afip=qr_afip,
    )

    fields = invoice["extraccion_enriquecida"]["campos"]
    assert fields["fecha_emision"]["valor"] == "2026-06-13"
    assert fields["fecha_emision"]["fuente"] == "qr"
    assert fields["punto_venta"]["valor"] == "0004"
    assert fields["numero_comprobante"]["valor"] == "00000099"
    assert fields["total"]["valor"] == "1300.50"
    assert any(v["codigo"] == "QR_OCR_MISMATCH" for v in invoice["extraccion_enriquecida"]["validaciones"]["fallas"])
    assert invoice["estado"] == "REVIEW_REQUIRED"


def test_missing_cuit_returns_empty_field_and_review_required() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT.replace("CUIT: 30-50000007-0\n", ""),
        source_type="webhook",
        original_filename="sin_cuit.pdf",
        mime_type="application/pdf",
        sha256="c" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT.replace("CUIT: 30-50000007-0\n", ""),
    )

    field = invoice["extraccion_enriquecida"]["campos"]["proveedor_cuit"]
    assert field == {
        "valor": None,
        "confianza": 0,
        "fuente": "vacio",
        "metodo": "not_found",
        "evidencia": "",
    }
    assert invoice["estado"] == "REVIEW_REQUIRED"
    assert invoice["validaciones"]["requiere_revision"] is True


def test_unbalanced_total_requires_review() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT.replace("Total: $ 1245,00", "Total: $ 9999,00"),
        source_type="webhook",
        original_filename="descuadrada.pdf",
        mime_type="application/pdf",
        sha256="d" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT.replace("Total: $ 1245,00", "Total: $ 9999,00"),
    )

    balance = invoice["extraccion_enriquecida"]["validaciones"]["balance_importes"]
    assert balance["ok"] is False
    assert Decimal(balance["diferencia"]) == Decimal("8754.00")
    assert invoice["estado"] == "REVIEW_REQUIRED"


def test_process_invoice_upload_reuses_existing_hash_without_force(monkeypatch) -> None:
    data = b"same invoice bytes"
    existing = {
        "status": "OK",
        "sha256": sha256_bytes(data),
        "json_file": "/tmp/existing.json",
        "xml_file": "/tmp/existing.xml",
        "ready_file": "/tmp/existing.ready",
        "requires_review": False,
        "staging": {"enabled": True, "ok": True, "factura_id": 12},
    }
    calls = {"extract": 0}

    monkeypatch.setattr(service, "find_existing_invoice_result", lambda sha: existing)

    def fail_extract(*args: object, **kwargs: object) -> tuple[str, str]:
        calls["extract"] += 1
        raise AssertionError("duplicate should not be reprocessed")

    monkeypatch.setattr(service, "extract_text", fail_extract)

    result = service.process_invoice_upload(
        data=data,
        source_type="email",
        original_filename="duplicada.pdf",
        mime_type="application/pdf",
        source_metadata={},
    )

    assert result == existing | {"duplicate": True}
    assert calls["extract"] == 0
