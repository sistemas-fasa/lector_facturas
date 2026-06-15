from __future__ import annotations

from decimal import Decimal

from invoice_parser_helpers import (
    _advanced_line_items,
    _replace_invoice_staging_iibb_perceptions,
    _sane_iibb_amount,
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
