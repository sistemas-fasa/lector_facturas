from __future__ import annotations

from decimal import Decimal

import json
import os
from pathlib import Path

from invoice_parser_helpers import (
    INVOICE_PROVIDER_PROFILES_ENV_VAR,
    _advanced_line_items,
    _apply_provider_profile,
    _extract_afip_comprobante_code,
    _extract_amount_after_label,
    _extract_date_after_label,
    _extract_text_after_label,
    _load_provider_profiles,
    _looks_like_bad_business_name,
    _replace_invoice_staging_detail,
    _replace_invoice_staging_iibb_perceptions,
    _sane_iibb_amount,
    _select_provider_profile,
    build_invoice_json,
    extract_total,
)
from factura_ocr.extract import extract_invoice_data


class FakeCursor:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple | list | None]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | list | None = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> dict:
        return self.rows.pop(0) if self.rows else {}


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.cursor_obj = FakeCursor(rows)

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


def test_sane_iibb_amount_discards_unbounded_ocr_identifier() -> None:
    advanced_invoice = {"perceptions_iibb": "9042807692"}

    amount = _sane_iibb_amount(
        advanced_invoice,
        total=0.0,
        cuit="30-71471099-7",
        reference_amounts=[0.0, -3.0, 0.0],
    )

    assert amount == 0.0


def test_replace_invoice_staging_iibb_perceptions_persists_jurisdiction_detail() -> None:
    conn = FakeConnection(rows=[{"importada": 0}])
    invoice = {
        "origen": {"sha256": "abc"},
        "percepciones_iibb_detalle": [
            {"jurisdiccion": "Misiones", "codjur": "914", "importe": 99.93},
            {"jurisdiccion": "", "codjur": "", "importe": 10.0},
            {"jurisdiccion": "Corrientes", "codjur": "905", "importe": 0.0},
        ],
    }

    rows = _replace_invoice_staging_iibb_perceptions(conn, 7, invoice)

    assert rows == 1
    executed_sql = [sql for sql, _params in conn.cursor_obj.statements]
    assert any("DELETE FROM facturas_ocr_percepciones_iibb" in sql for sql in executed_sql)
    inserts = [
        params
        for sql, params in conn.cursor_obj.statements
        if "INSERT INTO facturas_ocr_percepciones_iibb" in sql
    ]
    assert inserts == [(7, "abc", 1, "Misiones", "914", 99.93)]


def test_replace_invoice_staging_detail_falls_back_when_items_have_no_amounts() -> None:
    conn = FakeConnection(rows=[{"importada": 0}])
    invoice = {
        "origen": {"sha256": "abc"},
        "emisor": {"razon_social": "NATIONAL PLASTIC GROUP S.R.L."},
        "importes": {"neto_gravado": 652248.95, "total": 810810.67},
        "contabilidad": {
            "cuenta_contable": "501100",
            "cuenta_descripcion": "COMPRAS",
            "origen_sugerencia": "historico_stock_co",
            "score_sugerencia": 90,
            "requiere_confirmacion": False,
        },
        "items": [
            {"descripcion": "1 CAJA - TEE TRIPLE ENCHUFE", "cantidad": 3402.45, "precio_unitario": None, "subtotal": None},
            {"descripcion": "250 U CAJA - CODO DOBLE ENCHUFE", "cantidad": 941.67, "precio_unitario": None, "subtotal": None},
            {"descripcion": "U 3 1 K6", "cantidad": None, "precio_unitario": None, "subtotal": None},
        ],
        "extraccion_facturas_ocr": {},
    }

    rows = _replace_invoice_staging_detail(conn, 27, invoice)

    assert rows == 1
    inserts = [
        params
        for sql, params in conn.cursor_obj.statements
        if "INSERT INTO facturas_ocr_detalle" in sql
    ]
    assert len(inserts) == 1
    assert inserts[0][0:3] == (27, "abc", "NATIONAL PLASTIC GROUP S.R.L.")
    assert inserts[0][3] == 652248.95
    assert inserts[0][4:6] == ("501100", "COMPRAS")


def test_embedded_factura_ocr_extracts_dgr_codjur_for_misiones() -> None:
    invoice = extract_invoice_data(
        """FACTURA A
SUB. TOTAL Rg 3337
% IB.Mision
3,31
99,93
TOTAL: $ 3753,05
"""
    )

    assert invoice.perceptions_iibb == Decimal("99.93")
    assert len(invoice.perceptions_iibb_detail) == 1
    assert invoice.perceptions_iibb_detail[0].jurisdiction == "Misiones"
    assert invoice.perceptions_iibb_detail[0].codjur == "914"


def test_embedded_factura_ocr_discards_fragmented_duplicate_iibb_amount() -> None:
    invoice = extract_invoice_data(
        """FACTURA A
SUBTOTAL : 848.892,00
TASA GRAL. : 178.267,32
Percep IIBB Misiones: 21.222,30
Percep IIBB Misiones: 222,30
TOTAL : 1.048.381,62
"""
    )

    assert invoice.perceptions_iibb == Decimal("21222.30")
    assert len(invoice.perceptions_iibb_detail) == 1
    assert invoice.perceptions_iibb_detail[0].jurisdiction == "Misiones"
    assert invoice.perceptions_iibb_detail[0].codjur == "914"
    assert invoice.perceptions_iibb_detail[0].amount == Decimal("21222.30")


def test_embedded_factura_ocr_extracts_bare_percepciones_with_jurisdiction_next_line() -> None:
    invoice = extract_invoice_data(
        """FACTURA A
Subtotal 516.616,73
Percepcion I.V.A. 15.498,50
Percepciones
Misiones 3,31 % 17.100,01
TOTAL $ 657.704,75
"""
    )

    assert invoice.perceptions_iibb == Decimal("17100.01")
    assert len(invoice.perceptions_iibb_detail) == 1
    assert invoice.perceptions_iibb_detail[0].jurisdiction == "Misiones"
    assert invoice.perceptions_iibb_detail[0].codjur == "914"
    assert invoice.line_items == []


def test_embedded_factura_ocr_does_not_sum_repeated_iibb_aliquot_as_amount() -> None:
    invoice = extract_invoice_data(
        """FACTURA A
Subtotal 516.616,73
Percepcion I.V.A. 15.498,50
Percepciones
Misiones 3,31 % 17.100,01
Percepciones
Misiones 3,31 %
TOTAL $ 657.704,75
"""
    )

    assert invoice.perceptions_iibb == Decimal("17100.01")
    assert len(invoice.perceptions_iibb_detail) == 1
    assert invoice.perceptions_iibb_detail[0].amount == Decimal("17100.01")


def test_extract_total_prefers_currency_total_over_small_intermediate_total() -> None:
    total = extract_total(
        """Articulo Descripcion Cantidad P. Unit % Bonif. SubTotal
Talles: 040= 5, 041= 5, 044= 2, 12 ARS 50,604.00 0.00 ARS 607,248.00
Total: 32
SubTotal ARS 956,988.00
IVA 21% 21.00 899,568.72 188,909.43
Total IVA: ARS 188,909.43
Total: ARS 1,088,478.15
"""
    )

    assert total == 1088478.15


def test_build_invoice_json_populates_afip_comprobante_codigo_from_text() -> None:
    invoice = build_invoice_json(
        ocr_text=(
            "NATIONAL PLASTIC GROUP S.R.L.\n"
            "CUIT: 30-71536394-8\n"
            "FACTURA A\n"
            "Codigo: 001\n"
            "Punto de Venta: 0006 Comp. Nro: 00005780\n"
            "Fecha de Emision: 19/06/2026\n"
            "Total: $ 810.810,67\n"
        ),
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="ddeeff00" + "4" * 56,
        ocr_engine="pdf_text",
    )

    assert invoice["comprobante"]["codigo"] == "001"
    assert invoice["comprobante"]["letra"] == "A"
    assert invoice["comprobante"]["punto_venta"] == "0006"
    assert invoice["comprobante"]["numero"] == "00005780"


def test_build_invoice_json_populates_afip_comprobante_codigo_from_qr() -> None:
    invoice = build_invoice_json(
        ocr_text=(
            "NOTA DE CREDITO A\n"
            "CUIT: 30-50052655-2\n"
            "Punto de Venta: 0013 Comp. Nro: 00213556\n"
            "Fecha de Emision: 18/06/2026\n"
            "Total: $ 200.120,26\n"
        ),
        source_type="email",
        original_filename="nc.pdf",
        mime_type="application/pdf",
        sha256="eeff0011" + "5" * 56,
        ocr_engine="pdf_text",
        qr_afip={"datos": {"tipoCmp": 3, "ptoVta": 13, "nroCmp": 213556, "importe": 200120.26}},
    )

    assert invoice["comprobante"]["codigo"] == "003"
    assert invoice["comprobante"]["letra"] == "A"


def test_extract_afip_comprobante_code_finds_near_header_code() -> None:
    assert _extract_afip_comprobante_code("FACTURA A\nCod. 001\nNro 0006-00005780") == "001"


def test_bad_business_name_detects_ocr_labels() -> None:
    assert _looks_like_bad_business_name("Codigo: 001")
    assert _looks_like_bad_business_name("Numero: 0005-00007403")
    assert _looks_like_bad_business_name("Domicilio: FACUNDO QUIROGA 298")


def test_advanced_line_items_replaces_placeholder_unit_price_for_single_quantity() -> None:
    items = _advanced_line_items(
        {
            "line_items": [
                {
                    "description": "Talles: 040= 5, 041= 5, 044= 2",
                    "quantity": "1",
                    "unit_price": "1",
                    "subtotal": "349.74000",
                }
            ]
        }
    )

    assert items == [
        {
            "descripcion": "Talles: 040= 5, 041= 5, 044= 2",
            "cantidad": 1.0,
            "precio_unitario": 349.74,
            "subtotal": 349.74,
        }
    ]


def test_advanced_line_items_skips_fiscal_summary_rows() -> None:
    items = _advanced_line_items(
        {
            "line_items": [
                {"description": "Percepciones", "quantity": "15498.50", "unit_price": "17100.01", "subtotal": "657704.75"},
                {"description": "I.V.A. 21,00 % 108.489,51", "quantity": "", "unit_price": "", "subtotal": ""},
                {"description": "Bonificaciones", "quantity": "1", "unit_price": "1548.96", "subtotal": "10.00"},
                {"description": "PT-CAN-000-0360011 VENECIA BEIGE", "quantity": "48", "unit_price": "21830.45", "subtotal": "1047861.60"},
            ]
        }
    )

    assert items == [
        {
            "descripcion": "PT-CAN-000-0360011 VENECIA BEIGE",
            "cantidad": 48.0,
            "precio_unitario": 21830.45,
            "subtotal": 1047861.6,
        }
    ]


# ========== Provider profiles ==========

PROFILE_JSON = json.dumps({
    "proveedores": {
        "30500000070": {
            "nombre": "Proveedor Demo S.A.",
            "aliases": ["PROVEEDOR DEMO", "DEMO SA"],
            "campos": {
                "total": ["TOTAL A PAGAR", "IMPORTE TOTAL"],
                "neto_gravado": ["NETO GRAVADO"],
                "iva_21": ["IVA 21%"],
                "percepciones_iibb": ["PERC. IIBB"],
                "cae": ["CAE"],
                "vencimiento_cae": ["VTO CAE"],
            },
            "tolerancia_total": "1.00",
        },
        "30777777771": {
            "nombre": "Distribuidora Ejemplo SRL",
            "aliases": ["DISTRIBUIDORA EJEMPLO"],
            "campos": {
                "total": ["TOTAL COMPROBANTE"],
                "neto_gravado": ["NETO"],
            },
        },
    },
    "aliases_globales": {
        "DEMO SA": "30500000070",
    },
})

SAMPLE_OCR = (
    "PROVEEDOR DEMO SA\n"
    "CUIT: 30-50000007-0\n"
    "FACTURA A 0003-00000042\n"
    "NETO GRAVADO: $ 1000,00\n"
    "IVA 21%: $ 210,00\n"
    "PERC. IIBB: $ 30,00\n"
    "TOTAL A PAGAR: $ 1240,00\n"
    "CAE: 12345678901234\n"
    "VTO CAE: 30/06/2026\n"
)


# --- carga de perfiles ---

def test_load_profiles_no_env_var(monkeypatch) -> None:
    monkeypatch.delenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, raising=False)
    assert _load_provider_profiles() == {}


def test_load_profiles_file_not_found(monkeypatch) -> None:
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, "/no/existe/profiles.json")
    assert _load_provider_profiles() == {}


def test_load_profiles_invalid_json(monkeypatch, tmp_path) -> None:
    f = tmp_path / "profiles.json"
    f.write_text("{invalid json}", encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    assert _load_provider_profiles() == {}


def test_load_profiles_valid_json(monkeypatch, tmp_path) -> None:
    f = tmp_path / "profiles.json"
    f.write_text(PROFILE_JSON, encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    data = _load_provider_profiles()
    assert "proveedores" in data
    assert "30500000070" in data["proveedores"]


def test_load_profiles_not_a_dict(monkeypatch, tmp_path) -> None:
    f = tmp_path / "profiles.json"
    f.write_text('["not a dict"]', encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    assert _load_provider_profiles() == {}


# --- selección de perfil ---

def test_select_profile_by_qr_cuit() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, "30500000070", None, "")
    assert method == "qr_cuit"
    assert profile["nombre"] == "Proveedor Demo S.A."


def test_select_profile_by_text_cuit() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, None, "30-50000007-0", "")
    assert method == "text_cuit"
    assert profile["nombre"] == "Proveedor Demo S.A."


def test_select_profile_by_alias() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, None, None, "PROVEEDOR DEMO SA factura")
    assert method == "alias"
    assert profile["nombre"] == "Proveedor Demo S.A."


def test_select_profile_qr_prioritary_over_alias() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, "30500000070", None, "PROVEEDOR DEMO SA")
    assert method == "qr_cuit"


def test_select_profile_ambiguous_alias() -> None:
    data = {
        "proveedores": {
            "11111111111": {"nombre": "A", "aliases": ["ALIAS COMUN"]},
            "22222222222": {"nombre": "B", "aliases": ["ALIAS COMUN"]},
        },
    }
    profile, method, cuit = _select_provider_profile(data, None, None, "TEXTO CON ALIAS COMUN")
    assert method == "alias_ambiguo"
    assert profile is None


def test_select_profile_no_match() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, None, None, "TEXTO IRRELEVANTE")
    assert method == "sin_perfil"
    assert profile is None


# --- extracción por perfil ---

def test_extract_amount_after_label() -> None:
    val = _extract_amount_after_label("TOTAL A PAGAR: $ 1240,00", "TOTAL A PAGAR")
    assert val == 1240.00


def test_extract_amount_after_label_no_match() -> None:
    val = _extract_amount_after_label("SIN IMPORTE", "TOTAL")
    assert val is None


def test_extract_date_after_label() -> None:
    val = _extract_date_after_label("VTO CAE: 30/06/2026", "VTO CAE")
    assert val == "30/06/2026"


def test_extract_date_after_label_no_match() -> None:
    val = _extract_date_after_label("SIN FECHA", "VTO CAE")
    assert val is None


def test_extract_text_after_label() -> None:
    val = _extract_text_after_label("CAE: 12345678901234", "CAE")
    assert val == "12345678901234"


def test_apply_profile_extracts_total() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos.get("total", {}).get("valor") == "1240.00"
    assert campos["total"]["fuente"] == "perfil_proveedor"


def test_apply_profile_extracts_neto_gravado() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos.get("neto_gravado", {}).get("valor") == "1000.00"


def test_apply_profile_extracts_iva_21() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos.get("iva_21", {}).get("valor") == "210.00"


def test_apply_profile_extracts_percepciones_iibb() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos.get("percepciones_iibb", {}).get("valor") == "30.00"


def test_apply_profile_extracts_cae() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos.get("cae", {}).get("valor") == "12345678901234"


def test_apply_profile_does_not_override_high_confidence_qr() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos = {
        "total": {"valor": "1245.00", "confianza": 98, "fuente": "qr", "metodo": "qr_importe", "evidencia": ""},
    }
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos["total"]["valor"] == "1245.00"
    assert campos["total"]["fuente"] == "qr"


def test_apply_profile_fills_empty_field() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos = {
        "total": {"valor": None, "confianza": 0, "fuente": "vacio", "metodo": "not_found", "evidencia": ""},
    }
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert campos["total"]["valor"] == "1240.00"
    assert campos["total"]["fuente"] == "perfil_proveedor"


def test_apply_profile_has_evidence() -> None:
    data = json.loads(PROFILE_JSON)
    profile = data["proveedores"]["30500000070"]
    campos: dict = {}
    _apply_provider_profile(
        enriched_campos=campos,
        ocr_text=SAMPLE_OCR,
        pdf_text="",
        profile=profile,
        match_method="qr_cuit",
        matched_cuit="30500000070",
    )
    assert "etiqueta" in (campos.get("total", {}).get("evidencia") or "")
    assert campos["total"]["metodo"] == "profile_label_total"


# --- perfil en build_invoice_json ---

def test_build_invoice_json_with_profile(monkeypatch, tmp_path) -> None:
    f = tmp_path / "profiles.json"
    f.write_text(PROFILE_JSON, encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    invoice = build_invoice_json(
        ocr_text=SAMPLE_OCR,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="aabbccdd" + "0" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_OCR,
    )
    extraccion = invoice.get("extraccion_enriquecida") or {}
    perfil = extraccion.get("perfil_proveedor_aplicado")
    assert perfil is not None, "perfil_proveedor_aplicado deberia estar presente"
    assert perfil["cuit"] in ("30500000070",)
    assert perfil["matched_by"] in ("text_cuit",)


def test_build_invoice_json_without_profile_has_no_perfil_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, raising=False)
    invoice = build_invoice_json(
        ocr_text=SAMPLE_OCR,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="bbccddee" + "1" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_OCR,
    )
    extraccion = invoice.get("extraccion_enriquecida") or {}
    assert "perfil_proveedor_aplicado" not in extraccion


def test_build_invoice_json_invalid_profile_does_not_break(monkeypatch, tmp_path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{invalid json}", encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    invoice = build_invoice_json(
        ocr_text=SAMPLE_OCR,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="ccddeeff" + "2" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_OCR,
    )
    assert invoice["estado"] in ("OK", "REVIEW_REQUIRED")


# --- extractores unitarios ---

def test_extract_amount_after_label_with_pesos() -> None:
    assert _extract_amount_after_label("Total: $ 1500,50", "Total") == 1500.50


def test_extract_amount_after_label_with_colon() -> None:
    assert _extract_amount_after_label("NETO GRAVADO: 850,75", "NETO GRAVADO") == 850.75


def test_extract_amount_after_label_with_hash() -> None:
    assert _extract_amount_after_label("TOTAL A PAGAR# 2000,00", "TOTAL A PAGAR") == 2000.00


def test_extract_date_after_label_with_dash() -> None:
    assert _extract_date_after_label("Vencimiento CAE: 15-07-2026", "Vencimiento CAE") == "15/07/2026"


def test_extract_text_after_label_no_colon() -> None:
    val = _extract_text_after_label("CAE 12345678901234", "CAE")
    assert val is None


def test_global_alias_matches() -> None:
    data = json.loads(PROFILE_JSON)
    profile, method, cuit = _select_provider_profile(data, None, None, "FACTURA DE DEMO SA")
    assert method == "alias"
    assert cuit == "30500000070"


def test_profile_diagnostico_includes_perfil(monkeypatch, tmp_path) -> None:
    f = tmp_path / "profiles.json"
    f.write_text(PROFILE_JSON, encoding="utf-8")
    monkeypatch.setenv(INVOICE_PROVIDER_PROFILES_ENV_VAR, str(f))
    invoice = build_invoice_json(
        ocr_text=SAMPLE_OCR,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="eeff0011" + "3" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_OCR,
    )
    diag = invoice.get("diagnostico") or {}
    perfil = diag.get("perfil_proveedor_aplicado")
    assert perfil is not None
    assert "cuit" in perfil
