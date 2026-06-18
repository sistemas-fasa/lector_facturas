"""Helpers for invoice OCR parsing and Visual FoxPro handoff files.

The parser intentionally favors conservative extraction. Real invoice layouts
should be added as supplier-specific rules once production samples are known.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote
from xml.etree import ElementTree as ET


CUIT_RE = re.compile(r"\b(?:CUIT|C\.?U\.?I\.?T\.?)?\s*[:#-]?\s*(\d{2}[- ]?\d{8}[- ]?\d)\b", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
INVOICE_NUMBER_RE = re.compile(
    r"\b(?:Factura|Comp\.?|Comprobante|N(?:ro|[uú]mero)?\.?)?\s*"
    r"(?:[A-Z]\s*)?(?:N\s*[:#-]?\s*)?(\d{4,5})\s*[- ]\s*(\d{6,10})\b",
    re.I,
)
INVOICE_NUMBER_ALT_RE = re.compile(
    r"(?:Punto\s+de\s+Venta|Pto\.?\s*Vta\.?)\s*:\s*(\d{4,5}).*?Comp\.?\s*Nro\.?\s*:\s*(\d{6,10})",
    re.I | re.S,
)
DEFAULT_TOTAL_TOLERANCE = Decimal(os.environ.get("INVOICE_TOTAL_TOLERANCE", "1.00"))
ENRICHED_FIELD_NAMES = [
    "proveedor_nombre",
    "proveedor_cuit",
    "tipo_comprobante",
    "punto_venta",
    "numero_comprobante",
    "fecha_emision",
    "moneda",
    "neto_gravado",
    "iva_21",
    "iva_105",
    "iva_27",
    "iva_total",
    "percepciones_iibb",
    "percepciones_iva",
    "otros_tributos",
    "total",
    "codjur",
    "cae",
    "vencimiento_cae",
    "items",
]
CRITICAL_ENRICHED_FIELDS = [
    "proveedor_cuit",
    "tipo_comprobante",
    "punto_venta",
    "numero_comprobante",
    "fecha_emision",
    "total",
]


def normalize_text_encoding(text: Any) -> str:
    """Repair common mojibake and keep text in composed UTF-8 form."""
    value = "" if text is None else str(text)
    candidates = [value]
    if any(marker in value for marker in ("Ã", "Â", "â€", "ï¿½")):
        for encoding in ("latin-1", "cp1252"):
            try:
                candidates.append(value.encode(encoding).decode("utf-8"))
            except UnicodeError:
                pass
    best = min(candidates, key=_mojibake_score)
    return unicodedata.normalize("NFC", best)


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in ("Ã", "Â", "â€", "�", "ï¿½"))


def normalize_amount(text: Any) -> float:
    """Return a JSON-safe number using dot decimals from AR formatted text."""
    if text is None:
        return 0.0
    raw = str(text).strip()
    if not raw:
        return 0.0

    raw = raw.replace("$", "").replace("ARS", "").replace(" ", "")
    raw = re.sub(r"[^0-9,.\-]", "", raw)
    if not raw:
        return 0.0

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")

    try:
        return float(Decimal(raw).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return 0.0


def normalize_amount_decimal(text: Any) -> Decimal | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    raw = raw.replace("$", "").replace("ARS", "").replace(" ", "")
    raw = re.sub(r"[^0-9,.\-]", "", raw)
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def normalize_date(text: Any) -> str | None:
    """Normalize common invoice date strings to YYYY-MM-DD."""
    if text is None:
        return None
    value = str(text)
    iso_match = ISO_DATE_RE.search(value)
    if iso_match:
        return iso_match.group(0)

    match = DATE_RE.search(value)
    if not match:
        return None

    day, month, year = match.groups()
    if len(year) == 2:
        year = f"20{year}"
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return None


def _extract_issue_date(text: str) -> str | None:
    return _extract_date_by_patterns(
        text,
        [
            r"Fecha\s+(?:de\s+)?Emisi[oó]n\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Fecha\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ],
    ) or normalize_date(text)


def extract_cuit(text: str) -> str:
    candidates = re.findall(r"\b\d{2}[- ]?\d{8}[- ]?\d\b|\b\d{11}\b", text or "")
    for candidate in candidates:
        digits = re.sub(r"\D", "", candidate)
        if _valid_cuit_digits(digits):
            return _format_cuit(digits)
    match = CUIT_RE.search(text or "")
    if not match:
        return ""
    digits = re.sub(r"\D", "", match.group(1))
    return _format_cuit(digits) if len(digits) == 11 else digits


def extract_invoice_number(text: str) -> dict[str, str | None]:
    body = text or ""
    letter_match = re.search(
        r"\bFactura\s+([ABCM])\b|\bTipo\s*[:#-]?\s*([ABCM])\b|^\s*([ABCM])\s+Factura\b",
        body,
        re.I | re.M,
    )
    if not letter_match:
        letter_match = re.search(r"^\s*(?:[A-ZÁÉÍÓÚÑ0-9 .,&'-]+?)\s+([ABCM])\s+FACTURA\b", body, re.I | re.M)
    if not letter_match:
        letter_match = re.search(r"^\s*(?:[A-ZÁÉÍÓÚÑ0-9 .,&'-]+?)\s+([ABCM])\s*$[\s\S]{0,160}?\bFACTURA\b", body[:800], re.I | re.M)
    number_match = _best_invoice_number_match(body)
    letter = None
    if letter_match:
        letter = next(group for group in letter_match.groups() if group)
    return {
        "letra": letter.upper() if letter else None,
        "punto_venta": number_match.group(1).zfill(4) if number_match else "",
        "numero": number_match.group(2).zfill(8) if number_match else "",
    }


def _best_invoice_number_match(body: str) -> re.Match[str] | None:
    explicit = INVOICE_NUMBER_ALT_RE.search(body or "")
    if explicit:
        return explicit
    matches = list(INVOICE_NUMBER_RE.finditer(body or ""))
    if not matches:
        return None

    def score(match: re.Match[str]) -> int:
        start, end = match.span()
        context = (body or "")[max(0, start - 120) : min(len(body or ""), end + 120)]
        upper = context.upper()
        points = 0
        if re.search(r"FACTURA|COMPROBANTE|N[º°RO.]|COD", upper):
            points += 40
        if re.search(r"TEL|TE\s*:|TE\.|VEND|CLIENTE", upper):
            points -= 30
        if start < 900:
            points += 15
        return points

    best = max(matches, key=score)
    return best if score(best) > 0 else None


def extract_total(text: str) -> float:
    explicit_total_candidates = []
    for line in (text or "").splitlines():
        if not re.search(r"\b(?:Importe\s+)?Total\b", line, re.I):
            continue
        if re.search(r"cantidad|descripci[oó]n|precio\s+unit|%desc|unitario|cae|iva", line, re.I):
            continue
        total_pos = re.search(r"\b(?:Importe\s+)?Total\b", line, re.I)
        if not total_pos:
            continue
        tail = line[total_pos.end() :]
        amounts = [
            normalize_amount(amount)
            for amount in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", tail)
        ]
        amounts = [amount for amount in amounts if amount > 0]
        if amounts:
            explicit_total_candidates.append((line, amounts[-1]))

    currency_totals = [
        amount
        for line, amount in explicit_total_candidates
        if re.search(r"\b(?:ARS|USD|U\$S|\$)\b|\$", line, re.I)
    ]
    if currency_totals:
        return currency_totals[-1]
    if explicit_total_candidates:
        return explicit_total_candidates[-1][1]

    total_candidates = []
    for match in re.finditer(r"\bTOTAL\b", text or "", re.I):
        context = (text or "")[max(0, match.start() - 80) : match.end() + 80]
        if re.search(r"cantidad|descripci[oó]n|precio\s+unit|%desc|unitario", context, re.I):
            continue
        window = re.split(r"\b(?:CAE|C\.?A\.?E|Fecha\s+de\s+Vto|Comprobante\s+Autorizado)\b", (text or "")[match.end() : match.end() + 120], maxsplit=1, flags=re.I)[0]
        for amount in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", window):
            value = normalize_amount(amount)
            if value > 100:
                total_candidates.append(value)
    if total_candidates:
        return total_candidates[-1]

    patterns = [
        r"\bTotal\s*(?:a\s*pagar)?\s*[:$ ]+\s*([0-9][0-9.,]*)",
        r"\bImporte\s*total\s*[:$ ]+\s*([0-9][0-9.,]*)",
    ]
    return _extract_amount_by_patterns(text, patterns)


def extract_iva(text: str, rate: str = "21") -> float:
    table_values = _extract_tax_table_values(text, rate)
    if table_values.get("iva", 0.0) > 0:
        return table_values["iva"]

    escaped = re.escape(rate).replace("\\.", r"[,.]")
    lines = (text or "").splitlines()
    for idx, line in enumerate(lines):
        normalized = unicodedata.normalize("NFKD", line or "")
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).upper()
        iva_match = re.search(r"\bI\W*V\W*A\b", normalized)
        if not iva_match:
            continue
        rate_match = re.search(rf"\b{escaped}\s*%", normalized, re.I)
        if not rate_match:
            continue
        same_line_tail = line[rate_match.end() :]
        same_line_candidates = [
            normalize_amount(token)
            for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", same_line_tail)
        ]
        if same_line_candidates:
            return same_line_candidates[-1]
        window_parts = [line[iva_match.end() :]]
        for following in lines[idx + 1 : min(len(lines), idx + 6)]:
            if re.search(r"\b(?:VTO|VIO|CAE|TOTAL|SUBTOTAL|PERC)\b", following, re.I):
                break
            window_parts.append(following)
        window = " ".join(window_parts)
        candidates = []
        for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", window):
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", token):
                continue
            value = normalize_amount(token)
            if value > 100:
                candidates.append(value)
        if candidates:
            return candidates[-1]

    patterns = [
        rf"\bI\W*V\W*A\W*{escaped}\s*%\s*[:$ ]+\s*([0-9][0-9.,]*)",
        rf"\b{escaped}\s*%\s*I\W*V\W*A\s*[:$ ]+\s*([0-9][0-9.,]*)",
    ]
    if str(rate) == "21":
        patterns.extend(
            [
                r"\bTasa\s+Gral\.?\s*[:$ ]+\s*([0-9][0-9.,]*)",
                r"\bTasa\s+General\s*[:$ ]+\s*([0-9][0-9.,]*)",
            ]
        )
    return _extract_amount_by_patterns(text, patterns)


def _extract_tax_table_values(text: str, rate: str = "21") -> dict[str, float]:
    escaped = re.escape(rate).replace("\\.", r"[,.]")
    lines = (text or "").splitlines()
    for idx, line in enumerate(lines):
        normalized = unicodedata.normalize("NFKD", line or "")
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).upper()
        if not all(token in normalized for token in ("BASE", "IVA", "IMP")):
            continue
        for data_line in lines[idx + 1 : min(len(lines), idx + 4)]:
            rate_match = re.search(rf"\b{escaped}(?:[,.]0+)?\s*%", data_line, re.I)
            if not rate_match:
                continue
            before = [
                normalize_amount(token)
                for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", data_line[: rate_match.start()])
            ]
            after = [
                normalize_amount(token)
                for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", data_line[rate_match.end() :])
            ]
            base_values = [value for value in before if value > 0]
            tax_values = [value for value in after if value > 0]
            if base_values and tax_values:
                return {
                    "base": base_values[-1],
                    "iva": tax_values[0],
                    "subtotal": tax_values[-1] if len(tax_values) > 1 else 0.0,
                }
    return {}


def _extract_iva_taxable_base(text: str, rate: str = "21") -> float:
    table_values = _extract_tax_table_values(text, rate)
    if table_values.get("base", 0.0) > 0:
        return table_values["base"]

    escaped = re.escape(rate).replace("\\.", r"[,.]")
    for line in (text or "").splitlines():
        normalized = unicodedata.normalize("NFKD", line or "")
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).upper()
        if not re.search(r"\bI\W*V\W*A\b", normalized):
            continue
        rate_match = re.search(rf"\b{escaped}\b", normalized, re.I)
        if not rate_match:
            continue
        tail = line[rate_match.end() :]
        values = [
            normalize_amount(token)
            for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", tail)
        ]
        material_values = [value for value in values if value > 100]
        if len(material_values) >= 2:
            return material_values[-2]
    return 0.0


def _extract_iva_positive_text(text: str, rate: str) -> str | None:
    amount = normalize_amount_decimal(extract_iva(text, rate))
    if amount is None or amount <= 0:
        return None
    return f"{amount:.2f}"


def _empty_field() -> dict[str, Any]:
    return {
        "valor": None,
        "confianza": 0,
        "fuente": "vacio",
        "metodo": "not_found",
        "evidencia": "",
    }


def _field(value: Any, confidence: int, source: str, method: str, evidence: str) -> dict[str, Any]:
    if value in (None, ""):
        return _empty_field()
    return {
        "valor": value,
        "confianza": int(confidence),
        "fuente": source,
        "metodo": method,
        "evidencia": (evidence or "").strip()[:500],
    }


def _field_from_qr(value: Any, method: str, qr_data: dict[str, Any]) -> dict[str, Any]:
    return _field(value, 98, "qr", method, json.dumps(qr_data, ensure_ascii=False, sort_keys=True))


def _source_texts(pdf_text: str, ocr_text: str) -> list[tuple[str, str, int]]:
    texts: list[tuple[str, str, int]] = []
    if pdf_text.strip():
        texts.append(("pdf_text", normalize_text_encoding(pdf_text), 85))
    if ocr_text.strip() and ocr_text.strip() != pdf_text.strip():
        texts.append(("ocr", normalize_text_encoding(ocr_text), 70))
    if not texts and ocr_text.strip():
        texts.append(("ocr", normalize_text_encoding(ocr_text), 70))
    return texts


def _line_evidence(text: str, pattern: str) -> str:
    for line in (text or "").splitlines():
        if re.search(pattern, line, re.I):
            return line.strip()
    match = re.search(pattern, text or "", re.I | re.S)
    return re.sub(r"\s+", " ", match.group(0)).strip()[:180] if match else ""


def _total_evidence(text: str) -> str:
    candidates: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        if not re.search(r"\b(?:Importe\s+)?Total\b", line, re.I):
            continue
        if re.search(r"subtotal|total\s+iva|iva\s+total|cantidad|descripci[oó]n|precio\s+unit|%desc|unitario|cae", line, re.I):
            continue
        score = 0
        if re.search(r"total\s+a\s+pagar|importe\s+total|total\s+final", line, re.I):
            score += 40
        if re.search(r"\$|\bARS\b|\bUSD\b|U\$S", line, re.I):
            score += 10
        candidates.append((score, line.strip()))
    if not candidates:
        return _line_evidence(text, r"\bTotal\b|Importe\s+Total")
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _first_text_field(
    pdf_text: str,
    ocr_text: str,
    extractor: Any,
    method: str,
    evidence_pattern: str,
    *,
    formatter: Any | None = None,
) -> dict[str, Any]:
    for source, text, confidence in _source_texts(pdf_text, ocr_text):
        value = extractor(text)
        if formatter is not None and value not in (None, ""):
            value = formatter(value)
        if value not in (None, "", 0, 0.0):
            evidence = _total_evidence(text) if method == "regex_total" else _line_evidence(text, evidence_pattern)
            evidence = evidence or str(value)
            return _field(value, confidence, source, method, evidence)
    return _empty_field()


def _decimal_text(value: Any) -> str | None:
    amount = normalize_amount_decimal(value)
    return f"{amount:.2f}" if amount is not None else None


def _format_point(value: Any) -> str | None:
    digits = _digits_from_value(value)
    return digits.zfill(4) if digits else None


def _format_number(value: Any) -> str | None:
    digits = _digits_from_value(value)
    return digits.zfill(8) if digits else None


def _format_invoice_type_from_qr(value: Any) -> str | None:
    letter = _invoice_letter_from_afip_type(value)
    return f"FACTURA_{letter}" if letter else None


def _format_legacy_type(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    letter_match = re.search(r"\bFACTURA\s+([ABCM])\b|\b([ABCM])\s+FACTURA\b", raw)
    if letter_match:
        letter = next(group for group in letter_match.groups() if group)
        return f"FACTURA_{letter}"
    if "NOTA_CREDITO" in raw or "NOTA DE CREDITO" in raw or "NOTA DE CRÉDITO" in raw:
        return "NOTA_CREDITO"
    if "NOTA_DEBITO" in raw or "NOTA DE DEBITO" in raw or "NOTA DE DÉBITO" in raw:
        return "NOTA_DEBITO"
    if "FACTURA" in raw:
        return "FACTURA"
    return raw


def _extract_provider_name_for_enriched(text: str) -> str:
    advanced = _extract_with_facturas_ocr(text, "")
    if advanced and advanced.get("provider_name"):
        return str(advanced.get("provider_name") or "").strip()
    return _guess_business_name(text)


def _extract_invoice_letter_type(text: str) -> str | None:
    invoice_number = extract_invoice_number(text)
    letter = invoice_number.get("letra")
    if letter:
        return f"FACTURA_{letter}"
    return _format_legacy_type(_extract_comprobante_type(text))


def _extract_point(text: str) -> str | None:
    return extract_invoice_number(text).get("punto_venta") or None


def _extract_number(text: str) -> str | None:
    return extract_invoice_number(text).get("numero") or None


def _extract_importe_otros(text: str) -> str | None:
    return _decimal_text(_extract_amount_by_patterns(text, [r"\bOtros\s*(?:impuestos|tributos)\s*[:$ ]+\s*([0-9][0-9.,]*)"]))


def _extract_neto_gravado_text(text: str) -> str | None:
    explicit = _extract_amount_by_patterns(
        text,
        [
            r"\bImporte\s+Neto\s+Gravado\s*[:$ ]+\s*([0-9][0-9.,]*)",
            r"\bNeto\s*(?:gravado)?\s*[:$ ]+\s*([0-9][0-9.,]*)",
        ],
    )
    if explicit > 0:
        return _decimal_text(explicit)

    iva_base = _extract_iva_taxable_base(text, "21")
    if iva_base > 0:
        return _decimal_text(iva_base)

    return _decimal_text(
        _extract_amount_by_patterns(
            text,
            [
                r"\bSub\s*total\s*[:$ ]+\s*([0-9][0-9.,]*)",
                r"\bSubtotal\s*[:$ ]+\s*([0-9][0-9.,]*)",
                r"(?<![A-Za-z])Subt\.?\s*[:$ ]+\s*([0-9][0-9.,]*)",
            ],
        )
    )


def _extract_percepciones_iva(text: str) -> str | None:
    return _decimal_text(
        _extract_amount_by_patterns(
            text,
            [r"\bPercepci[oó]n\s+I\.?\s*V\.?\s*A\.?\s*[:$ ]+\s*([0-9][0-9.,]*)", r"\bPercepciones?\s+IVA\s*[:$ ]+\s*([0-9][0-9.,]*)"],
        )
    )


def _extract_cae_due(text: str) -> str | None:
    return _extract_date_by_patterns(
        text,
        [
            r"Fecha\s+Vto\.?\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Fecha\s+de\s+Vto\.?\s+de\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Vto\.?\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ],
    )


def _build_enriched_extraction(
    *,
    legacy_invoice: dict[str, Any],
    ocr_text: str,
    pdf_text: str,
    qr_afip: dict[str, Any] | None,
) -> dict[str, Any]:
    legacy_snapshot = json.loads(json.dumps(legacy_invoice, ensure_ascii=False, default=str))
    qr_data = _qr_afip_data(qr_afip)
    fields = {name: _empty_field() for name in ENRICHED_FIELD_NAMES}

    if qr_data:
        qr_type = _format_invoice_type_from_qr(qr_data.get("tipoCmp"))
        if qr_type:
            fields["tipo_comprobante"] = _field_from_qr(qr_type, "qr_tipo_cmp", qr_data)
        qr_point = _format_point(qr_data.get("ptoVta"))
        if qr_point:
            fields["punto_venta"] = _field_from_qr(qr_point, "qr_pto_vta", qr_data)
        qr_number = _format_number(qr_data.get("nroCmp"))
        if qr_number:
            fields["numero_comprobante"] = _field_from_qr(qr_number, "qr_nro_cmp", qr_data)
        qr_date = normalize_date(qr_data.get("fecha"))
        if qr_date:
            fields["fecha_emision"] = _field_from_qr(qr_date, "qr_fecha", qr_data)
        qr_cuit = _cuit_from_qr(qr_data.get("cuit"))
        if qr_cuit:
            fields["proveedor_cuit"] = _field_from_qr(qr_cuit, "qr_cuit", qr_data)
        qr_total = _decimal_text(qr_data.get("importe"))
        if qr_total:
            fields["total"] = _field_from_qr(qr_total, "qr_importe", qr_data)
        qr_currency = _currency_from_qr(qr_data.get("moneda"))
        if qr_currency:
            fields["moneda"] = _field_from_qr(qr_currency, "qr_moneda", qr_data)
        qr_cae = str(qr_data.get("codAut") or "").strip()
        if qr_cae:
            fields["cae"] = _field_from_qr(qr_cae, "qr_cod_aut", qr_data)

    text_field_specs = {
        "proveedor_nombre": (_extract_provider_name_for_enriched, "rule_provider_name", r"Raz[oó]n\s+Social|^[A-ZÁÉÍÓÚÜÑ][^\n]{4,160}$", None),
        "proveedor_cuit": (_extract_emisor_cuit, "regex_cuit", r"C[UÜVLI1|]{1,3}T|[0-9]{2}[- ]?[0-9]{8}[- ]?[0-9]", None),
        "tipo_comprobante": (_extract_invoice_letter_type, "regex_tipo_comprobante", r"FACTURA|NOTA\s+DE\s+CR[EÉ]DITO|NOTA\s+DE\s+D[EÉ]BITO", None),
        "punto_venta": (_extract_point, "regex_punto_venta", r"Punto\s+de\s+Venta|Pto\.?\s*Vta|[0-9]{4,5}\s*[- ]\s*[0-9]{6,10}", None),
        "numero_comprobante": (_extract_number, "regex_numero_comprobante", r"Comp\.?\s*Nro|Nro|[0-9]{4,5}\s*[- ]\s*[0-9]{6,10}", None),
        "fecha_emision": (_extract_issue_date, "regex_fecha_emision", r"Fecha\s+(?:de\s+)?Emisi[oó]n|Fecha\s*:", None),
        "moneda": (_extract_currency, "regex_moneda", r"\bARS\b|\bUSD\b|U\$S|\$", None),
        "neto_gravado": (_extract_neto_gravado_text, "regex_neto_gravado", r"Neto\s+Gravado|Importe\s+Neto|Sub\s*total|Subtotal|Subt\.", None),
        "iva_21": (lambda t: _extract_iva_positive_text(t, "21"), "regex_iva_21", r"I\W*V\W*A.*21|21.*I\W*V\W*A", None),
        "iva_105": (lambda t: _extract_iva_positive_text(t, "10.5"), "regex_iva_105", r"I\W*V\W*A.*10[,.]5|10[,.]5.*I\W*V\W*A", None),
        "iva_27": (lambda t: _extract_iva_positive_text(t, "27"), "regex_iva_27", r"I\W*V\W*A.*27|27.*I\W*V\W*A", None),
        "percepciones_iva": (_extract_percepciones_iva, "regex_percepciones_iva", r"Percepci[oó]n\s+I\.?\s*V\.?\s*A\.?|Percepciones?\s+IVA", None),
        "otros_tributos": (_extract_importe_otros, "regex_otros_tributos", r"Otros\s*(?:impuestos|tributos)", None),
        "total": (lambda t: _decimal_text(extract_total(t)), "regex_total", r"\bTotal\b|Importe\s+Total", None),
        "cae": (_extract_cae, "regex_cae", r"\bC\.?\s*A\.?\s*E\.?\b", None),
        "vencimiento_cae": (_extract_cae_due, "regex_vencimiento_cae", r"Vto\.?\s+CAE|Fecha\s+Vto\.?\s+CAE|Fecha\s+de\s+Vto", None),
    }
    for name, (extractor, method, evidence_pattern, formatter) in text_field_specs.items():
        if fields[name]["fuente"] == "qr":
            continue
        fields[name] = _first_text_field(pdf_text, ocr_text, extractor, method, evidence_pattern, formatter=formatter)

    advanced = legacy_invoice.get("extraccion_facturas_ocr") or {}
    iibb_detail = legacy_invoice.get("percepciones_iibb_detalle") or []
    if iibb_detail:
        total_iibb = sum((normalize_amount_decimal(item.get("importe")) or Decimal("0.00")) for item in iibb_detail)
        evidence = "; ".join(f"{item.get('jurisdiccion') or ''} {item.get('codjur') or ''} {item.get('importe')}" for item in iibb_detail)
        fields["percepciones_iibb"] = _field(f"{total_iibb.quantize(Decimal('0.01')):.2f}", 80, "ocr", "factura_ocr_iibb_detail", evidence)
        codjur = str(iibb_detail[0].get("codjur") or "").strip()
        if codjur:
            fields["codjur"] = _field(codjur, 80, "ocr", "factura_ocr_iibb_detail", evidence)
    elif advanced.get("perceptions_iibb"):
        total_value = float(normalize_amount_decimal(fields["total"]["valor"]) or Decimal("0.00"))
        cuit_value = str(fields["proveedor_cuit"]["valor"] or "")
        reference_amounts = [
            float(normalize_amount_decimal(fields[name]["valor"]) or Decimal("0.00"))
            for name in ("neto_gravado", "iva_21", "iva_105", "iva_27")
        ]
        amount = _sane_iibb_amount(advanced, total_value, cuit_value, reference_amounts=reference_amounts)
        if amount > 0:
            amount_text = f"{Decimal(str(amount)).quantize(Decimal('0.01')):.2f}"
            fields["percepciones_iibb"] = _field(amount_text, 70, "ocr", "factura_ocr_iibb", str(advanced.get("perceptions_iibb")))

    _dedupe_iibb_and_otros_tributos(fields)

    iva_values = [
        normalize_amount_decimal(fields[name]["valor"])
        for name in ("iva_21", "iva_105", "iva_27")
        if fields[name]["valor"] is not None
    ]
    if iva_values:
        iva_total = sum(iva_values, Decimal("0.00")).quantize(Decimal("0.01"))
        evidence = " + ".join(str(fields[name]["valor"]) for name in ("iva_21", "iva_105", "iva_27") if fields[name]["valor"] is not None)
        fields["iva_total"] = _field(f"{iva_total:.2f}", 90, "calculado", "sum_iva_rates", evidence)

    items = legacy_invoice.get("items") or []
    if items:
        fields["items"] = _field(items, 70, "ocr", "factura_ocr_items", f"{len(items)} items")

    validations = _validate_enriched_fields(fields, qr_data, pdf_text, ocr_text)
    status = "OK" if validations["ok"] else "REVIEW_REQUIRED"
    return {
        "version": "2.0",
        "status": status,
        "fuentes": {
            "pdf_text": pdf_text,
            "ocr_text": ocr_text,
            "pdf_text_chars": len(pdf_text or ""),
            "ocr_text_chars": len(ocr_text or ""),
            "qr_detectado": bool(qr_data),
        },
        "campos": fields,
        "validaciones": validations,
        "legacy": legacy_snapshot,
    }


def _validate_enriched_fields(fields: dict[str, dict[str, Any]], qr_data: dict[str, Any], pdf_text: str, ocr_text: str) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for name in CRITICAL_ENRICHED_FIELDS:
        if fields[name]["valor"] in (None, ""):
            failures.append({"codigo": "MISSING_CRITICAL_FIELD", "campo": name, "detalle": "Campo critico sin evidencia"})
        elif int(fields[name]["confianza"] or 0) < 60:
            failures.append({"codigo": "LOW_CONFIDENCE", "campo": name, "detalle": "Confianza baja"})

    cuit = fields["proveedor_cuit"]["valor"]
    if cuit and not _valid_cuit_digits(re.sub(r"\D", "", str(cuit))):
        failures.append({"codigo": "INVALID_CUIT", "campo": "proveedor_cuit", "detalle": "CUIT con digito verificador invalido"})

    total = normalize_amount_decimal(fields["total"]["valor"])
    if total is None or total <= 0:
        failures.append({"codigo": "INVALID_TOTAL", "campo": "total", "detalle": "Total no numerico o cero"})

    balance = _validate_amount_balance(fields)
    if not balance["ok"]:
        failures.append({"codigo": "TOTAL_MISMATCH", "campo": "total", "detalle": f"Diferencia {balance['diferencia']}"})

    if qr_data:
        failures.extend(_validate_qr_against_text(fields, qr_data, pdf_text or ocr_text))

    return {
        "ok": not failures,
        "fallas": failures,
        "balance_importes": balance,
    }


def _dedupe_iibb_and_otros_tributos(fields: dict[str, dict[str, Any]]) -> None:
    otros = normalize_amount_decimal(fields["otros_tributos"]["valor"])
    iibb = normalize_amount_decimal(fields["percepciones_iibb"]["valor"])
    total = normalize_amount_decimal(fields["total"]["valor"])
    neto = normalize_amount_decimal(fields["neto_gravado"]["valor"]) or Decimal("0.00")
    iva_values = [
        normalize_amount_decimal(fields[name]["valor"]) or Decimal("0.00")
        for name in ("iva_21", "iva_105", "iva_27")
    ]
    if otros is None or otros <= 0 or iibb is None or iibb <= 0:
        return

    base = neto + sum(iva_values, Decimal("0.00"))
    if iibb == otros:
        fields["otros_tributos"] = _empty_field()
        return
    if total is None:
        return
    closes_with_otros = abs((base + otros) - total) <= DEFAULT_TOTAL_TOLERANCE
    closes_with_iibb = abs((base + iibb) - total) <= DEFAULT_TOTAL_TOLERANCE
    if closes_with_otros and not closes_with_iibb:
        fields["percepciones_iibb"] = _field(
            f"{otros.quantize(Decimal('0.01')):.2f}",
            max(int(fields["percepciones_iibb"]["confianza"] or 0), 80),
            fields["otros_tributos"]["fuente"],
            "otros_tributos_iibb_balance",
            fields["otros_tributos"]["evidencia"],
        )
        fields["otros_tributos"] = _empty_field()


def _validate_amount_balance(fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = normalize_amount_decimal(fields["total"]["valor"])
    component_names = ["neto_gravado", "iva_21", "iva_105", "iva_27", "percepciones_iibb", "percepciones_iva", "otros_tributos"]
    components: list[Decimal] = []
    for name in component_names:
        value = normalize_amount_decimal(fields[name]["valor"])
        if value is not None and fields[name]["fuente"] != "vacio":
            components.append(value)
    if total is None or not components:
        return {"ok": True, "calculado": None, "total": f"{total:.2f}" if total is not None else None, "diferencia": "0.00", "tolerancia": f"{DEFAULT_TOTAL_TOLERANCE:.2f}"}
    calculated = sum(components, Decimal("0.00")).quantize(Decimal("0.01"))
    difference = abs(total - calculated).quantize(Decimal("0.01"))
    return {
        "ok": difference <= DEFAULT_TOTAL_TOLERANCE,
        "calculado": f"{calculated:.2f}",
        "total": f"{total:.2f}",
        "diferencia": f"{difference:.2f}",
        "tolerancia": f"{DEFAULT_TOTAL_TOLERANCE:.2f}",
    }


def _validate_qr_against_text(fields: dict[str, dict[str, Any]], qr_data: dict[str, Any], text: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    comparisons = {
        "proveedor_cuit": _cuit_from_qr(qr_data.get("cuit")),
        "punto_venta": _format_point(qr_data.get("ptoVta")),
        "numero_comprobante": _format_number(qr_data.get("nroCmp")),
        "fecha_emision": normalize_date(qr_data.get("fecha")),
        "total": _decimal_text(qr_data.get("importe")),
    }
    text_values = {
        "proveedor_cuit": _first_text_field("", text, _extract_emisor_cuit, "regex_cuit", r"C[UÜVLI1|]{1,3}T|[0-9]{2}[- ]?[0-9]{8}[- ]?[0-9]")["valor"],
        "punto_venta": _first_text_field("", text, _extract_point, "regex_punto_venta", r"Punto\s+de\s+Venta|[0-9]{4,5}\s*[- ]\s*[0-9]{6,10}")["valor"],
        "numero_comprobante": _first_text_field("", text, _extract_number, "regex_numero_comprobante", r"Comp\.?\s*Nro|[0-9]{4,5}\s*[- ]\s*[0-9]{6,10}")["valor"],
        "fecha_emision": _first_text_field("", text, _extract_issue_date, "regex_fecha_emision", r"Fecha\s+(?:de\s+)?Emisi[oó]n|Fecha\s*:")["valor"],
        "total": _first_text_field("", text, lambda t: _decimal_text(extract_total(t)), "regex_total", r"\bTotal\b|Importe\s+Total")["valor"],
    }
    for field_name, qr_value in comparisons.items():
        text_value = text_values.get(field_name)
        if qr_value not in (None, "") and text_value not in (None, "") and str(qr_value) != str(text_value):
            failures.append(
                {
                    "codigo": "QR_OCR_MISMATCH",
                    "campo": field_name,
                    "qr": qr_value,
                    "texto": text_value,
                    "detalle": "QR AFIP difiere del texto extraido",
                }
            )
    return failures


def build_invoice_json(
    *,
    ocr_text: str,
    source_type: str,
    original_filename: str,
    mime_type: str,
    sha256: str,
    pdf_text: str = "",
    phash: str = "",
    duplicate: bool = False,
    duplicate_reason: str | None = None,
    ocr_confidence: float | None = 0.0,
    ocr_engine: str = "tesseract",
    qr_afip: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ocr_text = normalize_text_encoding(ocr_text)
    qr_data = _qr_afip_data(qr_afip)
    advanced_invoice = _extract_with_facturas_ocr(ocr_text, original_filename)
    invoice_number = extract_invoice_number(ocr_text)
    issue_date = _extract_issue_date(ocr_text)
    due_date = _extract_date_by_patterns(
        ocr_text,
        [
            r"Fecha\s+de\s+Vencimiento\s+de\s+Pago\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Vencimiento\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ],
    )
    total = extract_total(ocr_text)
    iva_21 = extract_iva(ocr_text, "21")
    iva_105 = extract_iva(ocr_text, "10.5")
    iva_27 = extract_iva(ocr_text, "27")
    cae = _extract_cae(ocr_text)
    cae_due_date = _extract_date_by_patterns(
        ocr_text,
        [
            r"\bCAE\s+\d{10,20}\s+Venc\.?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Fecha\s+Vto\.?\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Fecha\s+de\s+Vto\.?\s+de\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"Vto\.?\s+CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"V[ti]o\.?\s*\.?\s*CAE\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"CAE.*?Vto\.?\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ],
    )
    cuit = _extract_emisor_cuit(ocr_text) or extract_cuit(ocr_text)
    business_name = _guess_business_name(ocr_text)
    if advanced_invoice:
        if not business_name and advanced_invoice.get("provider_name"):
            business_name = str(advanced_invoice["provider_name"])[:120]
        if not cuit and advanced_invoice.get("cuit"):
            cuit = _format_cuit(advanced_invoice["cuit"])
        if not invoice_number["numero"] and advanced_invoice.get("invoice_number"):
            invoice_number = _invoice_number_from_advanced(str(advanced_invoice["invoice_number"])) or invoice_number
        if not issue_date and advanced_invoice.get("invoice_date"):
            issue_date = normalize_date(advanced_invoice["invoice_date"])
    if total <= 0:
        total = normalize_amount(advanced_invoice.get("total"))
        if iva_21 <= 0:
            iva_21 = normalize_amount(advanced_invoice.get("iva"))
    if qr_data:
        qr_invoice_number = _invoice_number_from_qr(qr_data)
        if qr_invoice_number:
            invoice_number = qr_invoice_number
        issue_date = normalize_date(qr_data.get("fecha")) or issue_date
        qr_cuit = _cuit_from_qr(qr_data.get("cuit"))
        if qr_cuit:
            cuit = qr_cuit
        qr_total = normalize_amount(qr_data.get("importe"))
        if qr_total > 0:
            total = qr_total
        qr_cae = str(qr_data.get("codAut") or "").strip()
        if qr_cae:
            cae = qr_cae
    if not invoice_number["letra"]:
        invoice_number["letra"] = _invoice_letter_from_ocr_afip_code(ocr_text)
    currency = _currency_from_qr(qr_data.get("moneda") if qr_data else None) or _extract_currency(ocr_text)
    neto_gravado = _extract_neto_gravado(ocr_text, total, iva_21, iva_105, iva_27, advanced_invoice)
    reference_amounts = [total, neto_gravado, iva_21, iva_105, iva_27]
    iibb_amount = _sane_iibb_amount(advanced_invoice, total, cuit, reference_amounts=reference_amounts)
    observations = []
    if not business_name:
        observations.append("Razon social del emisor no detectada en OCR")
    if not invoice_number["letra"]:
        observations.append("Letra del comprobante no detectada")
    if cae and not cae_due_date:
        observations.append("Vencimiento de CAE no detectado")
    requires_review = not all([total > 0, cuit, issue_date, invoice_number["numero"], business_name, invoice_number["letra"]])

    invoice = {
        "version": "1.0",
        "estado": "DUPLICADO" if duplicate else "OK",
        "fecha_proceso": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "origen": {
            "tipo": source_type,
            "archivo_original": original_filename,
            "mime_type": mime_type,
            "sha256": sha256,
            "phash": phash,
            "duplicado": duplicate,
            "motivo_duplicado": duplicate_reason,
            "email": (source_metadata or {}).get("email") or {},
        },
        "comprobante": {
            "tipo": _extract_comprobante_type(ocr_text),
            "letra": invoice_number["letra"],
            "punto_venta": invoice_number["punto_venta"],
            "numero": invoice_number["numero"],
            "fecha_emision": issue_date,
            "fecha_vencimiento": due_date,
            "moneda": currency,
            "cae": cae,
            "cae_vencimiento": cae_due_date,
        },
        "emisor": {
            "razon_social": business_name,
            "cuit": cuit,
            "iva_condicion": _extract_iva_condition(ocr_text),
            "domicilio": _extract_address(ocr_text),
        },
        "receptor": _extract_receptor(ocr_text),
        "importes": {
            "neto_gravado": neto_gravado,
            "iva_21": iva_21,
            "iva_105": iva_105,
            "iva_27": iva_27,
            "exento": _extract_amount_by_patterns(ocr_text, [r"\bExento\s*[:$ ]+\s*([0-9][0-9.,]*)"]),
            "no_gravado": _extract_amount_by_patterns(ocr_text, [r"\bNo\s*gravado\s*[:$ ]+\s*([0-9][0-9.,]*)"]),
            "percepciones": _extract_amount_by_patterns(ocr_text, [r"\bPercepciones?\s*[:$ ]+\s*([0-9][0-9.,]*)"])
            or iibb_amount,
            "percepciones_iibb": iibb_amount,
            "otros_impuestos": _extract_amount_by_patterns(ocr_text, [r"\bOtros\s*impuestos\s*[:$ ]+\s*([0-9][0-9.,]*)"]),
            "total": total,
        },
        "percepciones_iibb_detalle": _advanced_iibb_detail(advanced_invoice),
        "items": _advanced_line_items(advanced_invoice),
        "ocr": {
            "texto": ocr_text,
            "confianza": None if ocr_confidence is None else float(ocr_confidence),
            "motor": ocr_engine,
        },
        "qr_afip": qr_afip or {},
        "extraccion_facturas_ocr": advanced_invoice or {},
        "contabilidad": {},
        "validaciones": {
            "total_detectado": total > 0,
            "cuit_detectado": bool(cuit),
            "fecha_detectada": bool(issue_date),
            "numero_detectado": bool(invoice_number["numero"]),
            "qr_detectado": bool(qr_data),
            "requiere_revision": requires_review,
            "observaciones": observations or (["Revisar parser con formatos reales de factura"] if requires_review else []),
        },
    }
    enriched = _build_enriched_extraction(
        legacy_invoice=invoice,
        ocr_text=ocr_text,
        pdf_text=pdf_text,
        qr_afip=qr_afip,
    )
    invoice["extraccion_enriquecida"] = enriched
    if enriched["status"] != "OK" and invoice["estado"] != "DUPLICADO":
        invoice["estado"] = enriched["status"]
        invoice["validaciones"]["requiere_revision"] = True
        for failure in enriched["validaciones"]["fallas"]:
            detail = failure.get("detalle") or failure.get("codigo") or "Validacion enriquecida fallida"
            if detail not in invoice["validaciones"]["observaciones"]:
                invoice["validaciones"]["observaciones"].append(detail)
    invoice["contabilidad"] = infer_accounting(invoice, advanced_invoice)
    provider_label = str((invoice.get("contabilidad") or {}).get("proveedor_nombre") or "").strip()
    if provider_label and not (invoice.get("contabilidad") or {}).get("observaciones"):
        if _looks_like_bad_business_name(str(invoice["emisor"].get("razon_social") or "")):
            invoice["emisor"]["razon_social"] = provider_label[:120]
    invoice["diagnostico"] = build_diagnostico(invoice)
    return invoice


DIAGNOSTICO_FIELDS = [
    "proveedor_cuit",
    "tipo_comprobante",
    "punto_venta",
    "numero_comprobante",
    "fecha_emision",
    "total",
    "moneda",
    "cae",
    "vencimiento_cae",
    "neto_gravado",
    "iva_21",
    "iva_105",
    "iva_27",
    "percepciones_iibb",
    "percepciones_iva",
    "otros_tributos",
]


def build_diagnostico(invoice: dict[str, Any]) -> dict[str, Any]:
    enriched = invoice.get("extraccion_enriquecida") or {}
    validaciones = invoice.get("validaciones") or {}
    qr_afip = invoice.get("qr_afip") or {}
    origen = invoice.get("origen") or {}
    estado = invoice.get("estado") or "OK"
    requiere_revision = validaciones.get("requiere_revision", False) or estado == "REVIEW_REQUIRED"
    is_duplicate = estado == "DUPLICADO"

    campos_resumen: dict[str, Any] = {}
    encontrados: list[str] = []
    faltantes: list[str] = []

    enriched_campos = (enriched.get("campos") or {}) if enriched else {}
    for name in DIAGNOSTICO_FIELDS:
        field = enriched_campos.get(name) or {}
        valor = field.get("valor")
        if valor not in (None, ""):
            campos_resumen[name] = {
                "valor": valor,
                "fuente": field.get("fuente"),
                "confianza": field.get("confianza"),
                "metodo": field.get("metodo"),
                "evidencia": field.get("evidencia"),
            }
            encontrados.append(name)
        else:
            campos_resumen[name] = {}
            if name in (
                "proveedor_cuit",
                "tipo_comprobante",
                "punto_venta",
                "numero_comprobante",
                "fecha_emision",
                "total",
            ):
                faltantes.append(name)

    fallas = list((enriched.get("validaciones") or {}).get("fallas") or [])
    balance = (enriched.get("validaciones") or {}).get("balance_importes") or {}

    if not qr_afip.get("detectado"):
        pass

    if is_duplicate:
        recomendacion = "ignorar_duplicado"
    elif requiere_revision and not invoice.get("ocr", {}).get("texto", "").strip():
        recomendacion = "reintentar"
    elif requiere_revision:
        recomendacion = "revisar_manualmente"
    else:
        recomendacion = "aceptar"

    return {
        "requiere_revision": requiere_revision,
        "estado": estado,
        "qr_detectado": bool(qr_afip.get("detectado")),
        "pdf_text_chars": len(origen.get("pdf_text") or str(enriched.get("fuentes", {}).get("pdf_text") or "")),
        "ocr_text_chars": len(origen.get("ocr_text") or str(enriched.get("fuentes", {}).get("ocr_text") or "")),
        "campos_criticos": {
            "encontrados": encontrados,
            "faltantes": faltantes,
        },
        "campos": campos_resumen,
        "fallas": fallas,
        "balance_importes": balance,
        "recomendacion": recomendacion,
    }


def build_invoice_xml(invoice: dict[str, Any]) -> str:
    root = ET.Element("factura")
    _dict_to_xml(root, invoice)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def hamming_distance(hex1: str, hex2: str) -> int:
    if not hex1 or not hex2:
        return 9999
    try:
        return (int(hex1, 16) ^ int(hex2, 16)).bit_count()
    except ValueError:
        return 9999


def atomic_write_files(
    *,
    output_dir: str | os.PathLike[str],
    invoice: dict[str, Any],
    original_path: str | os.PathLike[str] | None = None,
    original_bytes: bytes | None = None,
    original_extension: str = "",
    generate_xml: bool = True,
    subdir: str | None = None,
) -> dict[str, str | None]:
    """Write JSON/XML with .tmp + rename + .ready protocol for VFP."""
    base_dir = Path(output_dir)
    if subdir:
        base_dir = base_dir / subdir
    originals_dir = Path(output_dir) / "originales"
    base_dir.mkdir(parents=True, exist_ok=True)
    originals_dir.mkdir(parents=True, exist_ok=True)

    sha256 = invoice["origen"]["sha256"]
    short_sha = sha256[:8]
    date_stamp = datetime.now().strftime("%Y%m%d")
    base_name = f"FACTURA_{date_stamp}_{short_sha}"
    json_path = base_dir / f"{base_name}.json"
    xml_path = base_dir / f"{base_name}.xml"
    ready_path = base_dir / f"{base_name}.ready"
    original_ext = (original_extension or Path(str(original_path or "")).suffix or ".bin").lstrip(".")
    original_dest = originals_dir / f"{sha256}.{original_ext}"

    _atomic_text_write(json_path, json.dumps(invoice, ensure_ascii=False, indent=2))
    xml_file: Path | None = None
    if generate_xml:
        _atomic_text_write(xml_path, build_invoice_xml(invoice))
        xml_file = xml_path

    if original_bytes is not None:
        _atomic_binary_write(original_dest, original_bytes)
    elif original_path:
        _atomic_copy(Path(original_path), original_dest)

    ready_path.write_bytes(b"")
    return {
        "json_file": str(json_path),
        "xml_file": str(xml_file) if xml_file else None,
        "ready_file": str(ready_path),
        "original_file": str(original_dest),
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_invoice_staging(invoice: dict[str, Any], written_files: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Persist parsed invoice into MySQL staging tables for FoxPro consumption."""
    result = {"enabled": False, "ok": False, "factura_id": None, "detalle_rows": 0, "percepcion_rows": 0, "error": ""}
    conn_info = _mysql_connection_info()
    if not conn_info:
        result["error"] = "Sin credenciales MySQL para staging"
        return result

    try:
        import pymysql  # type: ignore
    except Exception as exc:
        result["error"] = f"pymysql no disponible: {exc}"
        return result

    result["enabled"] = True
    try:
        conn = pymysql.connect(
            host=conn_info["host"],
            port=int(conn_info.get("port") or 3306),
            user=conn_info["user"],
            password=conn_info["password"],
            database=conn_info["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=4,
            read_timeout=8,
            write_timeout=8,
        )
    except Exception as exc:
        result["error"] = f"No se pudo conectar a MySQL para staging: {exc}"
        return result

    try:
        with conn:
            if not _table_exists(conn, "facturas_ocr_cabecera"):
                result["error"] = "No existe tabla facturas_ocr_cabecera"
                return result
            factura_id = _upsert_invoice_staging_header(conn, invoice, written_files or {})
            detail_rows = _replace_invoice_staging_detail(conn, factura_id, invoice)
            perception_rows = 0
            if _table_exists(conn, "facturas_ocr_percepciones_iibb"):
                perception_rows = _replace_invoice_staging_iibb_perceptions(conn, factura_id, invoice)
            _upsert_invoice_email_origin(conn, factura_id, invoice)
            _insert_invoice_staging_event(conn, factura_id, None, invoice, "ocr_recibido", "Factura recibida desde invoice-parser")
            conn.commit()
            result.update(
                {
                    "ok": True,
                    "factura_id": factura_id,
                    "detalle_rows": detail_rows,
                    "percepcion_rows": perception_rows,
                    "error": "",
                }
            )
            return result
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        result["error"] = f"Error guardando staging: {exc}"
        return result


def infer_accounting(invoice: dict[str, Any], advanced_invoice: dict[str, Any] | None = None) -> dict[str, Any]:
    """Suggest an accounting account using FASA history when DB credentials exist."""
    provider_name = invoice.get("emisor", {}).get("razon_social") or (advanced_invoice or {}).get("provider_name") or ""
    cuit = invoice.get("emisor", {}).get("cuit") or (advanced_invoice or {}).get("cuit") or ""
    description = _accounting_reference_description(invoice, advanced_invoice)
    amount = _accounting_reference_amount(invoice, advanced_invoice)
    empty = {
        "proveedor_codigo": "",
        "proveedor_nombre": provider_name,
        "cuenta_contable": "",
        "cuenta_descripcion": "",
        "origen_sugerencia": "sin_sugerencia",
        "score_sugerencia": 0.0,
        "descripcion_referencia": description,
        "importe_referencia": amount,
        "requiere_confirmacion": True,
        "observaciones": [],
    }

    conn_info = _mysql_connection_info()
    if not conn_info:
        empty["observaciones"].append("Sin credenciales MySQL en el sidecar para inferir cuenta contable")
        return empty

    try:
        import pymysql  # type: ignore
    except Exception as exc:
        empty["observaciones"].append(f"pymysql no disponible: {exc}")
        return empty

    try:
        conn = pymysql.connect(
            host=conn_info["host"],
            port=int(conn_info.get("port") or 3306),
            user=conn_info["user"],
            password=conn_info["password"],
            database=conn_info["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=4,
            read_timeout=8,
            write_timeout=8,
        )
    except Exception as exc:
        empty["observaciones"].append(f"No se pudo conectar a MySQL para inferencia contable: {exc}")
        return empty

    try:
        with conn:
            provider = _find_accounting_provider(conn, cuit, provider_name)
            if not provider:
                empty["observaciones"].append("Proveedor no encontrado en tabla proveedo")
                return empty
            provider_code = str(provider.get("PROVEEDOR") or "").strip()
            provider_label = str(provider.get("NOMBRE") or provider.get("DENO") or provider_name).strip()

            rule = _find_accounting_rule(conn, provider_code, description, amount)
            if rule:
                return {
                    **empty,
                    "proveedor_codigo": provider_code,
                    "proveedor_nombre": provider_label,
                    "cuenta_contable": str(rule.get("cuenta_contable") or "").strip(),
                    "cuenta_descripcion": str(rule.get("cuenta_descripcion") or "").strip(),
                    "origen_sugerencia": "regla",
                    "score_sugerencia": float(rule.get("confianza") or 0),
                    "requiere_confirmacion": bool(rule.get("requiere_revision")),
                    "observaciones": [],
                }

            historical = _find_historical_account(conn, provider_code)
            if historical:
                return {
                    **empty,
                    "proveedor_codigo": provider_code,
                    "proveedor_nombre": provider_label,
                    "cuenta_contable": str(historical.get("CLAVE") or "").strip(),
                    "cuenta_descripcion": str(historical.get("DESC0") or "").strip(),
                    "origen_sugerencia": "historico_stock_co",
                    "score_sugerencia": float(historical.get("SCORE") or 0),
                    "requiere_confirmacion": float(historical.get("SCORE") or 0) < 85,
                    "observaciones": [],
                }
            empty["proveedor_codigo"] = provider_code
            empty["proveedor_nombre"] = provider_label
            empty["observaciones"].append("Sin historico contable en stock_co para el proveedor")
            return empty
    except Exception as exc:
        empty["observaciones"].append(f"Error en inferencia contable: {exc}")
        return empty


def _extract_with_facturas_ocr(ocr_text: str, original_filename: str) -> dict[str, Any] | None:
    try:
        from factura_ocr.extract import extract_invoice_data
    except Exception:
        return None
    try:
        invoice = extract_invoice_data(ocr_text, source_file=original_filename)
        data = invoice.to_dict()
        provider = str(data.get("provider_name") or "")
        if re.search(r"\bFecha\b|\bEmisi[oó]n\b", provider, re.I):
            data["provider_name"] = _guess_business_name(ocr_text) or provider
        data["raw_text"] = invoice.raw_text
        return data
    except Exception as exc:
        return {"error": str(exc)}


def _advanced_line_items(advanced_invoice: dict[str, Any] | None) -> list[dict[str, Any]]:
    items = []
    for item in (advanced_invoice or {}).get("line_items", []) or []:
        quantity = _amount_or_none(item.get("quantity"))
        unit_price = _amount_or_none(item.get("unit_price"))
        subtotal = _amount_or_none(item.get("subtotal"))
        description = str(item.get("description") or "")
        if _is_fiscal_summary_line_item(description):
            continue
        if quantity is None and unit_price is None and subtotal is None:
            parsed = _parse_afip_line_item_description(description)
            if parsed:
                description = parsed["descripcion"]
                quantity = parsed["cantidad"]
                unit_price = parsed["precio_unitario"]
                subtotal = parsed["subtotal"]
        if quantity == 1.0 and unit_price == 1.0 and subtotal and subtotal > 1.0:
            unit_price = subtotal
        if not description and subtotal is None and unit_price is None:
            continue
        items.append(
            {
                "descripcion": description,
                "cantidad": quantity,
                "precio_unitario": unit_price,
                "subtotal": subtotal,
            }
        )
    return items


def _is_fiscal_summary_line_item(description: str) -> bool:
    clean = re.sub(r"\s+", " ", description or "").strip()
    return bool(
        re.search(
            r"^(?:percepciones?|percepci[oó]n|i\.?\s*v\.?\s*a\.?|bonificaciones?|subtotal|total)\b",
            clean,
            re.I,
        )
    )


def _parse_afip_line_item_description(description: str) -> dict[str, Any] | None:
    generic_pattern = re.compile(
        r"^(?P<cantidad>\d+(?:[,.]\d+)?)\s+"
        r"(?P<descripcion>.+?)\s+"
        r"(?P<precio_unitario>\d{1,3}(?:[.,]\d{3})*(?:[,.]\d+)?)\s+"
        r"(?P<bonificacion>\d+(?:[,.]\d+)?)\s+"
        r"(?P<subtotal>\d{1,3}(?:[.,]\d{3})*(?:[,.]\d+)?)"
        r"(?=\s+(?:REMITENTE|Exento|DESTINATARIO|DESDE|IVA|Sobre|Comprobante|$))",
        re.I,
    )
    clean = re.sub(r"\s+", " ", description or "").strip()
    generic_match = generic_pattern.match(clean)
    if generic_match:
        return {
            "descripcion": generic_match.group("descripcion").strip(),
            "cantidad": normalize_amount(generic_match.group("cantidad")),
            "precio_unitario": normalize_amount(generic_match.group("precio_unitario")),
            "subtotal": normalize_amount(generic_match.group("subtotal")),
        }

    pattern = re.compile(
        r"^(?P<descripcion>.+?)\s+"
        r"(?P<cantidad>\d+(?:[,.]\d+)?)\s+"
        r"(?P<unidad>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+)\s+"
        r"(?P<precio_unitario>\d+(?:[,.]\d+)?)\s+"
        r"(?P<bonificacion>\d+(?:[,.]\d+)?)\s+"
        r"(?P<subtotal>\d+(?:[,.]\d+)?)\s+"
        r"(?P<iva>\d+(?:[,.]\d+)?%)\s+"
        r"(?P<total>\d+(?:[,.]\d+)?)$",
        re.I,
    )
    match = pattern.match(clean)
    if not match:
        return None
    return {
        "descripcion": match.group("descripcion").strip(),
        "cantidad": normalize_amount(match.group("cantidad")),
        "precio_unitario": normalize_amount(match.group("precio_unitario")),
        "subtotal": normalize_amount(match.group("subtotal")),
    }


def _advanced_iibb_detail(advanced_invoice: dict[str, Any] | None) -> list[dict[str, Any]]:
    details = []
    for item in (advanced_invoice or {}).get("perceptions_iibb_detail", []) or []:
        amount = normalize_amount(item.get("amount"))
        jurisdiction = str(item.get("jurisdiction", "") or "").strip()
        codjur = str(item.get("codjur", "") or "").strip()
        if amount <= 0 or not (jurisdiction or codjur):
            continue
        details.append(
            {
                "jurisdiccion": jurisdiction,
                "codjur": codjur,
                "importe": amount,
            }
        )
    return details


def _amount_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return normalize_amount(value)


def _sane_iibb_amount(
    advanced_invoice: dict[str, Any] | None,
    total: float,
    cuit: str,
    *,
    reference_amounts: list[float | int | None] | None = None,
) -> float:
    amount = normalize_amount((advanced_invoice or {}).get("perceptions_iibb"))
    if amount <= 0:
        return 0.0
    cuit_digits = re.sub(r"\D", "", cuit or "")
    if cuit_digits and str(int(amount)) == cuit_digits:
        return 0.0
    if total > 0 and amount > total:
        return 0.0
    positive_refs = [float(value) for value in reference_amounts or [] if value is not None and float(value) > 0]
    if positive_refs and amount > max(positive_refs):
        return 0.0
    if not positive_refs and amount >= 1_000_000:
        return 0.0
    return amount


def _invoice_number_from_advanced(value: str) -> dict[str, str | None] | None:
    match = re.search(r"(\d{4,5})\s*[- ]\s*(\d{6,10})", value or "")
    if not match:
        return None
    return {"letra": None, "punto_venta": match.group(1).zfill(4), "numero": match.group(2).zfill(8)}


def _qr_afip_data(qr_afip: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(qr_afip, dict):
        return {}
    data = qr_afip.get("datos")
    return data if isinstance(data, dict) else {}


def _invoice_number_from_qr(data: dict[str, Any]) -> dict[str, str | None] | None:
    point = _digits_from_value(data.get("ptoVta"))
    number = _digits_from_value(data.get("nroCmp"))
    if not point or not number:
        return None
    return {
        "letra": _invoice_letter_from_afip_type(data.get("tipoCmp")),
        "punto_venta": point.zfill(4),
        "numero": number.zfill(8),
    }


def _invoice_letter_from_afip_type(value: Any) -> str | None:
    digits = _digits_from_value(value)
    if not digits:
        return None
    code = int(digits)
    if 1 <= code <= 5 or code in {201, 202, 203}:
        return "A"
    if 6 <= code <= 10 or code in {206, 207, 208}:
        return "B"
    if 11 <= code <= 15 or code in {211, 212, 213}:
        return "C"
    if 51 <= code <= 54:
        return "M"
    return None


def _invoice_letter_from_ocr_afip_code(text: str) -> str | None:
    head = (text or "")[:1200]
    patterns = [
        r"\bCod\.?\s*(?:igo)?\s*[:#-]?\s*(\d{1,3})\b",
        r"\bC[oó]digo\s+Tipo\s+Comprobante\s*[:#-]?\s*(\d{1,3})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, head, re.I)
        if match:
            return _invoice_letter_from_afip_type(match.group(1).zfill(3))
    cod_pos = re.search(r"\bCod\.?\b|\bC[oó]digo\b", head, re.I)
    if cod_pos and re.search(r"\bFACTURA\b", head[: cod_pos.start() + 80], re.I):
        window = head[cod_pos.end() : cod_pos.end() + 260]
        for code_match in re.finditer(r"\b(00[1-9]|01[0-5]|05[1-4]|20[1-3]|20[6-8]|21[1-3])\b", window):
            letter = _invoice_letter_from_afip_type(code_match.group(1))
            if letter:
                return letter
    return None


def _cuit_from_qr(value: Any) -> str:
    digits = _digits_from_value(value)
    if len(digits) != 11:
        return ""
    return _format_cuit(digits) if _valid_cuit_digits(digits) else ""


def _currency_from_qr(value: Any) -> str:
    code = str(value or "").strip().upper()
    if code in {"PES", "ARS"}:
        return "ARS"
    if code in {"DOL", "USD"}:
        return "USD"
    return code if len(code) == 3 else ""


def _digits_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\D", "", str(value))


def _format_cuit(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return f"{digits[:2]}-{digits[2:10]}-{digits[10:]}" if len(digits) == 11 else str(value or "")


def _valid_cuit_digits(digits: str) -> bool:
    if not re.fullmatch(r"\d{11}", digits or ""):
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(digit) * weight for digit, weight in zip(digits[:10], weights))
    remainder = 11 - (total % 11)
    check = 0 if remainder == 11 else 9 if remainder == 10 else remainder
    return check == int(digits[-1])


def _accounting_reference_description(invoice: dict[str, Any], advanced_invoice: dict[str, Any] | None) -> str:
    items = invoice.get("items") or []
    if items:
        return str(items[0].get("descripcion") or "")[:255]
    advanced_items = (advanced_invoice or {}).get("line_items") or []
    if advanced_items:
        return str(advanced_items[0].get("description") or "")[:255]
    return str(invoice.get("emisor", {}).get("razon_social") or "")[:255]


def _accounting_reference_amount(invoice: dict[str, Any], advanced_invoice: dict[str, Any] | None) -> float:
    items = invoice.get("items") or []
    if items:
        amount = normalize_amount(items[0].get("subtotal"))
        if amount > 0:
            return amount
    for key in ("neto_gravado", "total"):
        amount = normalize_amount(invoice.get("importes", {}).get(key))
        if amount > 0:
            return amount
    for key in ("subtotal", "total"):
        amount = normalize_amount((advanced_invoice or {}).get(key))
        if amount > 0:
            return amount
    return 0.0


def _mysql_connection_info() -> dict[str, str] | None:
    url = os.environ.get("FASA_MYSQL_URL") or os.environ.get("MYSQL_URL") or os.environ.get("DATABASE_URL")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in {"mysql", "mariadb"}:
            return None
        return {
            "host": parsed.hostname or "",
            "port": str(parsed.port or 3306),
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": (parsed.path or "/").lstrip("/") or "fasa",
        }
    host = os.environ.get("FASA_MYSQL_HOST") or os.environ.get("MYSQL_HOST")
    user = os.environ.get("FASA_MYSQL_USER") or os.environ.get("MYSQL_USER")
    password = os.environ.get("FASA_MYSQL_PASSWORD") or os.environ.get("MYSQL_PASSWORD")
    database = os.environ.get("FASA_MYSQL_DATABASE") or os.environ.get("MYSQL_DATABASE") or "fasa"
    if not (host and user and password):
        return None
    return {
        "host": host,
        "port": os.environ.get("FASA_MYSQL_PORT") or os.environ.get("MYSQL_PORT") or "3306",
        "user": user,
        "password": password,
        "database": database,
    }


def _find_accounting_provider(conn: Any, cuit: str, provider_name: str) -> dict[str, Any] | None:
    digits = re.sub(r"\D", "", cuit or "")
    with conn.cursor() as cur:
        if digits:
            cur.execute(
                """
                SELECT PROVEEDOR, NOMBRE, DENO, CUIT
                FROM proveedo
                WHERE REPLACE(REPLACE(REPLACE(CUIT, '-', ''), ' ', ''), '.', '') = %s
                LIMIT 1
                """,
                (digits,),
            )
            row = cur.fetchone()
            if row:
                return row
        name = re.sub(r"\s+", " ", provider_name or "").strip().upper()
        if name:
            like = f"%{name[:80]}%"
            cur.execute(
                """
                SELECT PROVEEDOR, NOMBRE, DENO, CUIT
                FROM proveedo
                WHERE UPPER(NOMBRE) LIKE %s OR UPPER(DENO) LIKE %s
                LIMIT 1
                """,
                (like, like),
            )
            return cur.fetchone()
    return None


def _table_exists(conn: Any, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table_name,),
        )
        row = cur.fetchone() or {}
    return int(row.get("n") or 0) > 0


def _table_columns(conn: Any, table_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table_name,),
        )
        rows = cur.fetchall() or []
    return {str(row.get("COLUMN_NAME") or "") for row in rows}


def _legacy_invoice_for_storage(invoice: dict[str, Any]) -> dict[str, Any]:
    legacy = dict(invoice)
    legacy.pop("extraccion_enriquecida", None)
    return legacy


def _upsert_invoice_staging_header(conn: Any, invoice: dict[str, Any], written_files: dict[str, str | None]) -> int:
    origin = invoice.get("origen") or {}
    comp = invoice.get("comprobante") or {}
    issuer = invoice.get("emisor") or {}
    receiver = invoice.get("receptor") or {}
    amounts = invoice.get("importes") or {}
    accounting = invoice.get("contabilidad") or {}
    validations = invoice.get("validaciones") or {}

    values = {
        "sha256": origin.get("sha256") or "",
        "archivo_original": origin.get("archivo_original") or "",
        "json_file": written_files.get("json_file") or "",
        "xml_file": written_files.get("xml_file") or "",
        "ready_file": written_files.get("ready_file") or "",
        "estado": invoice.get("estado") or "OK",
        "requiere_revision": _bool_int(validations.get("requiere_revision")),
        "observaciones": "; ".join(validations.get("observaciones") or []),
        "fecha_proceso": _mysql_datetime(invoice.get("fecha_proceso")),
        "proveedor_codigo": accounting.get("proveedor_codigo") or "",
        "proveedor_nombre": accounting.get("proveedor_nombre") or "",
        "emisor_razon_social": issuer.get("razon_social") or "",
        "emisor_cuit": issuer.get("cuit") or "",
        "emisor_iva_condicion": issuer.get("iva_condicion") or "",
        "emisor_domicilio": issuer.get("domicilio") or "",
        "receptor_razon_social": receiver.get("razon_social") or "",
        "receptor_cuit": receiver.get("cuit") or "",
        "receptor_iva_condicion": receiver.get("iva_condicion") or "",
        "comprobante_tipo": comp.get("tipo") or "FACTURA",
        "letra": comp.get("letra") or None,
        "punto_venta": comp.get("punto_venta") or "",
        "numero": comp.get("numero") or "",
        "fecha_emision": comp.get("fecha_emision") or None,
        "fecha_vencimiento": comp.get("fecha_vencimiento") or None,
        "cae": comp.get("cae") or "",
        "cae_vencimiento": comp.get("cae_vencimiento") or None,
        "moneda": comp.get("moneda") or "ARS",
        "neto_gravado": normalize_amount(amounts.get("neto_gravado")),
        "iva_21": normalize_amount(amounts.get("iva_21")),
        "iva_105": normalize_amount(amounts.get("iva_105")),
        "iva_27": normalize_amount(amounts.get("iva_27")),
        "exento": normalize_amount(amounts.get("exento")),
        "no_gravado": normalize_amount(amounts.get("no_gravado")),
        "percepciones": normalize_amount(amounts.get("percepciones")),
        "percepciones_iibb": normalize_amount(amounts.get("percepciones_iibb")),
        "otros_impuestos": normalize_amount(amounts.get("otros_impuestos")),
        "total": normalize_amount(amounts.get("total")),
        "cuenta_contable_sugerida": accounting.get("cuenta_contable") or "",
        "cuenta_descripcion_sugerida": accounting.get("cuenta_descripcion") or "",
        "origen_sugerencia": accounting.get("origen_sugerencia") or "",
        "score_sugerencia": normalize_amount(accounting.get("score_sugerencia")),
        "requiere_confirmacion_contable": _bool_int(accounting.get("requiere_confirmacion")),
        "qr_detectado": _bool_int(validations.get("qr_detectado")),
        "ocr_motor": (invoice.get("ocr") or {}).get("motor") or "",
        "ocr_confianza": _amount_or_none((invoice.get("ocr") or {}).get("confianza")),
        "file_hash": origin.get("sha256") or "",
        "source_type": origin.get("tipo") or "",
        "archivo_nombre": origin.get("archivo_original") or "",
        "ocr_text": (invoice.get("ocr") or {}).get("texto") or "",
        "pdf_text": ((invoice.get("extraccion_enriquecida") or {}).get("fuentes") or {}).get("pdf_text") or "",
        "qr_raw": json.dumps(invoice.get("qr_afip") or {}, ensure_ascii=False),
        "extracted_json_enriched": json.dumps(invoice.get("extraccion_enriquecida") or {}, ensure_ascii=False, default=str),
        "extracted_json_legacy": json.dumps(_legacy_invoice_for_storage(invoice), ensure_ascii=False, default=str),
        "status": invoice.get("estado") or "OK",
        "error_message": "; ".join(validations.get("observaciones") or []),
    }
    if not values["sha256"]:
        raise ValueError("No se puede guardar staging sin sha256")

    existing_columns = _table_columns(conn, "facturas_ocr_cabecera")
    if existing_columns:
        values = {key: value for key, value in values.items() if key in existing_columns}
    columns = list(values)
    update_columns = [col for col in columns if col not in {"sha256"}]
    sql = f"""
        INSERT INTO facturas_ocr_cabecera ({', '.join(columns)})
        VALUES ({', '.join(['%s'] * len(columns))})
        ON DUPLICATE KEY UPDATE
          {', '.join([f'{col} = VALUES({col})' for col in update_columns])},
          id = LAST_INSERT_ID(id)
    """
    with conn.cursor() as cur:
        cur.execute(sql, [values[col] for col in columns])
        return int(cur.lastrowid)


def _replace_invoice_staging_detail(conn: Any, factura_id: int, invoice: dict[str, Any]) -> int:
    origin = invoice.get("origen") or {}
    accounting = invoice.get("contabilidad") or {}
    sha = origin.get("sha256") or ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT importada FROM facturas_ocr_cabecera WHERE id = %s",
            (factura_id,),
        )
        row = cur.fetchone() or {}
        if int(row.get("importada") or 0):
            return 0
        cur.execute("DELETE FROM facturas_ocr_detalle WHERE factura_id = %s", (factura_id,))
        count = 0
        for line_no, item in enumerate(invoice.get("items") or [], start=1):
            cur.execute(
                """
                INSERT INTO facturas_ocr_detalle (
                  factura_id, sha256, linea, descripcion_factura, cantidad,
                  precio_unitario, subtotal, cuenta_contable, cuenta_descripcion,
                  origen_sugerencia, score_sugerencia, requiere_confirmacion, confirmada
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
                """,
                (
                    factura_id,
                    sha,
                    line_no,
                    item.get("descripcion") or "",
                    _amount_or_none(item.get("cantidad")),
                    _amount_or_none(item.get("precio_unitario")),
                    _amount_or_none(item.get("subtotal")),
                    accounting.get("cuenta_contable") or "",
                    accounting.get("cuenta_descripcion") or "",
                    accounting.get("origen_sugerencia") or "",
                    normalize_amount(accounting.get("score_sugerencia")),
                    _bool_int(accounting.get("requiere_confirmacion")),
                ),
            )
            count += 1
        if count == 0:
            cur.execute(
                """
                INSERT INTO facturas_ocr_detalle (
                  factura_id, sha256, linea, descripcion_factura, cantidad,
                  precio_unitario, subtotal, cuenta_contable, cuenta_descripcion,
                  origen_sugerencia, score_sugerencia, requiere_confirmacion, confirmada
                )
                VALUES (%s, %s, 1, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, 0)
                """,
                (
                    factura_id,
                    sha,
                    _accounting_reference_description(invoice, invoice.get("extraccion_facturas_ocr") or {}),
                    normalize_amount((invoice.get("importes") or {}).get("neto_gravado"))
                    or normalize_amount((invoice.get("importes") or {}).get("total")),
                    accounting.get("cuenta_contable") or "",
                    accounting.get("cuenta_descripcion") or "",
                    accounting.get("origen_sugerencia") or "",
                    normalize_amount(accounting.get("score_sugerencia")),
                    _bool_int(accounting.get("requiere_confirmacion")),
                ),
            )
            count = 1
        return count


def _replace_invoice_staging_iibb_perceptions(conn: Any, factura_id: int, invoice: dict[str, Any]) -> int:
    origin = invoice.get("origen") or {}
    sha = origin.get("sha256") or ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT importada FROM facturas_ocr_cabecera WHERE id = %s",
            (factura_id,),
        )
        row = cur.fetchone() or {}
        if int(row.get("importada") or 0):
            return 0
        cur.execute("DELETE FROM facturas_ocr_percepciones_iibb WHERE factura_id = %s", (factura_id,))
        count = 0
        for line_no, item in enumerate(invoice.get("percepciones_iibb_detalle") or [], start=1):
            amount = _amount_or_none(item.get("importe"))
            jurisdiction = str(item.get("jurisdiccion") or "").strip()
            codjur = str(item.get("codjur") or "").strip()
            if amount is None or amount <= 0 or not (jurisdiction or codjur):
                continue
            count += 1
            cur.execute(
                """
                INSERT INTO facturas_ocr_percepciones_iibb (
                  factura_id, sha256, linea, jurisdiccion, codjur, importe
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (factura_id, sha, count, jurisdiction, codjur, amount),
            )
        return count


def _upsert_invoice_email_origin(conn: Any, factura_id: int, invoice: dict[str, Any]) -> None:
    if not _table_exists(conn, "facturas_ocr_email_origen"):
        return
    origin = invoice.get("origen") or {}
    email = origin.get("email") or {}
    if not email:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_ocr_email_origen (
              factura_id, sha256, message_id, email_from, email_to, email_subject,
              email_date, attachment_name
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              factura_id = VALUES(factura_id),
              email_from = VALUES(email_from),
              email_to = VALUES(email_to),
              email_subject = VALUES(email_subject),
              email_date = VALUES(email_date),
              attachment_name = VALUES(attachment_name)
            """,
            (
                factura_id,
                origin.get("sha256") or "",
                email.get("message_id") or "",
                email.get("from") or "",
                email.get("to") or "",
                email.get("subject") or "",
                email.get("date") or "",
                email.get("attachment_name") or "",
            ),
        )


def _insert_invoice_staging_event(
    conn: Any,
    factura_id: int | None,
    detalle_id: int | None,
    invoice: dict[str, Any],
    event: str,
    detail: str,
    user: str = "invoice-parser",
) -> None:
    sha = (invoice.get("origen") or {}).get("sha256") or ""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_ocr_eventos (factura_id, detalle_id, sha256, evento, detalle, usuario)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (factura_id, detalle_id, sha, event, detail, user),
        )


def _bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def _mysql_datetime(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _find_accounting_rule(conn: Any, provider_code: str, description: str, amount: float) -> dict[str, Any] | None:
    if not _table_exists(conn, "factura_reglas_contables"):
        return None
    normalized = _normalize_accounting_text(description)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patron_normalizado, cuenta_contable, cuenta_descripcion, confianza, requiere_revision
            FROM factura_reglas_contables
            WHERE activa = 1
              AND proveedor_codigo = %s
              AND (%s BETWEEN importe_min AND importe_max OR (importe_min = 0 AND importe_max = 0))
              AND (%s LIKE CONCAT('%%', patron_normalizado, '%%') OR patron_normalizado = '')
            ORDER BY prioridad ASC, confianza DESC, veces_confirmada DESC, id ASC
            LIMIT 1
            """,
            (provider_code, amount, normalized),
        )
        return cur.fetchone()


def _find_historical_account(conn: Any, provider_code: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              RTRIM(sc.CLAVE) AS CLAVE,
              COUNT(*) AS LINEAS_CLAVE,
              totals.TOTAL_LINEAS AS TOTAL_LINEAS,
              ROUND((COUNT(*) / NULLIF(totals.TOTAL_LINEAS, 0)) * 100, 2) AS SCORE,
              COALESCE(MAX(c.DESC0), '') AS DESC0
            FROM stock_co sc
            JOIN (
              SELECT COUNT(*) AS TOTAL_LINEAS
              FROM stock_co
              WHERE PROVEEDOR = %s AND RTRIM(COALESCE(CLAVE, '')) <> ''
            ) totals
            LEFT JOIN contable c ON c.CODIGO = sc.CLAVE
            WHERE sc.PROVEEDOR = %s AND RTRIM(COALESCE(sc.CLAVE, '')) <> ''
            GROUP BY RTRIM(sc.CLAVE), totals.TOTAL_LINEAS
            ORDER BY COUNT(*) DESC, SUM(sc.IMPORTE) DESC, RTRIM(sc.CLAVE)
            LIMIT 1
            """,
            (provider_code, provider_code),
        )
        return cur.fetchone()


def _normalize_accounting_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^0-9A-Z]+", " ", value.upper())
    return re.sub(r"\s+", " ", value).strip()


def _extract_amount_by_patterns(text: str, patterns: list[str]) -> float:
    body = text or ""
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            return normalize_amount(match.group(1))
    return 0.0


def _extract_comprobante_type(text: str) -> str:
    body = text or ""
    if re.search(r"\bNOTA\s+DE\s+CR[EÉ]DITO\b|\bN\.?\s*C\.?\b", body, re.I):
        return "NOTA_CREDITO"
    if re.search(r"\bNOTA\s+DE\s+D[EÉ]BITO\b|\bN\.?\s*D\.?\b", body, re.I):
        return "NOTA_DEBITO"
    return "FACTURA"


def _extract_neto_gravado(
    text: str,
    total: float,
    iva_21: float,
    iva_105: float,
    iva_27: float,
    advanced_invoice: dict[str, Any] | None,
) -> float:
    iva_total = normalize_amount(iva_21) + normalize_amount(iva_105) + normalize_amount(iva_27)
    explicit = _extract_amount_by_patterns(
        text,
        [
            r"\bImporte\s+Neto\s+Gravado\s*[:$ ]+\s*([0-9][0-9.,]*)",
            r"\bNeto\s*(?:gravado)?\s*[:$ ]+\s*([0-9][0-9.,]*)",
        ],
    )
    if explicit > 0:
        return explicit
    iva_base = _extract_iva_taxable_base(text, "21")
    if iva_base > 0 and total > 0 and iva_total > 0 and abs((iva_base + iva_total) - total) <= 0.05:
        return iva_base
    subtotal = _extract_amount_by_patterns(
        text,
        [
            r"\bSub\s*total\s*[:$ ]+\s*([0-9][0-9.,]*)",
            r"\bSubtotal\s*[:$ ]+\s*([0-9][0-9.,]*)",
            r"(?<![A-Za-z])Subt\.?\s*[:$ ]+\s*([0-9][0-9.,]*)",
        ],
    )
    if subtotal > 0:
        return subtotal
    advanced_subtotal = normalize_amount((advanced_invoice or {}).get("subtotal"))
    if advanced_subtotal > 0:
        return advanced_subtotal
    if total > iva_total > 0:
        return round(total - iva_total, 2)
    return 0.0


def _extract_currency(text: str) -> str:
    if re.search(r"\bUSD\b|U\$S|D[oó]lares", text or "", re.I):
        return "USD"
    return "ARS"


def _guess_business_name(text: str) -> str:
    match = re.search(r"Raz[oó]n\s+Social\s*:\s*(.+?)(?:\s{2,}|$)", text or "", re.I | re.M)
    if match:
        return match.group(1).strip()[:120]

    match = re.search(r"(?:^|\n)(?:PED\s*\n)?([A-ZÁÉÍÓÚÜÑ][^\n]{4,160}?)\s+CUIT\s*:", text or "", re.I)
    if match:
        candidate = match.group(1).strip()
        if not re.fullmatch(r"[ABC]", candidate, re.I):
            return candidate[:120]

    header = re.split(r"\b(?:CUIT|Cuit|Condicion\s+frente\s+al\s+IVA|Sr\.\(s\))\b", text or "", maxsplit=1)[0]
    for line in header.splitlines():
        clean = line.strip()
        if clean and not re.fullmatch(r"[ABC]", clean, re.I) and not re.search(
            r"factura|cod\.|original|pagina|cuit|iva|fecha|total|software|punto de venta|comp\.?\s*nro|^n[º°ro.]*\s*:|^sr\.\(s\)|direcci[oó]n|localidad|provincia|forma de pago|articulo|descripci[oó]n|cantidad|importe|subtotal|transporte",
            clean,
            re.I,
        ):
            return clean[:120]
    return ""


def _looks_like_bad_business_name(value: str) -> bool:
    clean = re.sub(r"\s+", " ", value or "").strip()
    if not clean:
        return True
    return bool(
        re.search(
            r"art[ií]culo|descripci[oó]n|cantidad|importe|unitario|factura\s+anticipada|^tecno\.?\s+[abcm]$|^s?rupo$",
            clean,
            re.I,
        )
    )


def _extract_date_by_patterns(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I | re.S)
        if match:
            return normalize_date(match.group(1))
    return None


def _extract_cae(text: str) -> str | None:
    patterns = [
        r"\bC\.?\s*A\.?\s*E\.?\s*(?:N[º°ro.]*)?\s*[:#-]?\s*(\d{10,20})\b",
        r"\bCAE\s*(?:N[º°ro.]*)?\s*[:#-]?\s*(\d{10,20})\b",
        r"\bCAE[-:\sA-Z]{0,8}(\d{10,20})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            return match.group(1)
    return None


def _extract_emisor_cuit(text: str) -> str:
    lines = (text or "").splitlines()
    first_invoice_idx = next(
        (
            idx
            for idx, line in enumerate(lines)
            if re.search(r"\bFACTURA\b|\bNOTA\s+DE\s+CR[EÉ]DITO\b|\bNOTA\s+DE\s+D[EÉ]BITO\b", line, re.I)
        ),
        None,
    )
    customer_start_idx = next(
        (
            idx
            for idx, line in enumerate(lines)
            if re.search(
                r"\b(?:CLIENTE|RECEPTOR|COMPRADOR|SEÑORES?|SENORES?|SR\.?\s*/?\s*\(?ES\)?|CUIT\s*:.*LISTA|COND\.?\s*VTA|LOCALIDAD\s*:|TRANSPORTE)\b|DOMICILIO\s*:",
                line,
                re.I,
            )
        ),
        None,
    )
    patterns = [
        r"\bC[UÜVLI1|]{1,3}T\s*[:#—-]?\s*(\d{2}[- ]?\d{8}[- ]?\d|\d{11})",
        r"\b(\d{2}[- ]?\d{8}[- ]?\d|\d{11})\b",
    ]
    scored: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        for pattern in patterns:
            for match in re.finditer(pattern, line, re.I):
                digits = re.sub(r"\D", "", match.group(1))
                if not _valid_cuit_digits(digits):
                    continue
                context = "\n".join(lines[max(0, idx - 2) : min(len(lines), idx + 3)])
                score = 0
                if re.search(r"\b(?:PROVEEDOR|EMISOR|RAZ[OÓ]N\s+SOCIAL|CONDICI[OÓ]N\s+FRENTE\s+AL\s+IVA|INGRESOS\s+BRUTOS)\b", context, re.I):
                    score += 40
                if re.search(
                    r"\b(?:CLIENTE|RECEPTOR|COMPRADOR|SEÑORES?|SENORES?|SR\.?\s*/?\s*\(?ES\)?|APELLIDO\s+Y\s+NOMBRE|LISTA|COND\.?\s*VTA|LOCALIDAD|TRANSPORTE|VENDEDOR)\b|DOMICILIO\s*:",
                    context,
                    re.I,
                ):
                    score -= 70
                if customer_start_idx is not None:
                    if idx < customer_start_idx:
                        score += 30 - min(customer_start_idx - idx, 10)
                    else:
                        score -= 60 + min(idx - customer_start_idx, 20)
                if first_invoice_idx is not None:
                    if idx <= first_invoice_idx:
                        score += 30
                        score -= abs(first_invoice_idx - idx)
                    else:
                        score -= 20 + min(idx - first_invoice_idx, 20)
                else:
                    score -= idx
                scored.append((score, -idx, digits))
    if scored:
        scored.sort(reverse=True)
        return _format_cuit(scored[0][2])

    issuer_text = re.split(r"\bSr\s*/?\s*\(?es\)?|\bSr\.\(s\)|\bCliente\b|\bDomicilio\s*:", text or "", maxsplit=1, flags=re.I)[0]
    for scope in (issuer_text, text or ""):
        for pattern in patterns:
            for match in re.finditer(pattern, scope, re.I):
                digits = re.sub(r"\D", "", match.group(1))
                if _valid_cuit_digits(digits):
                    return _format_cuit(digits)
    return ""


def _extract_iva_condition(text: str) -> str:
    match = re.search(r"Condici[oó]n\s+frente\s+al\s+IVA\s*:\s*(.+?)(?:\s{2,}|$)", text or "", re.I | re.M)
    return match.group(1).strip()[:80] if match else ""


def _extract_address(text: str) -> str:
    match = re.search(r"Domicilio\s+Comercial\s*:\s*(.+?)(?:\s{2,}|$)", text or "", re.I | re.M)
    if not match:
        return ""
    first = _clean_address_part(match.group(1))
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if "Domicilio Comercial" in line and index + 1 < len(lines):
            next_line = lines[index + 1].strip()
            if next_line:
                next_line = _clean_address_part(next_line)
                if next_line and not re.search(r"IVA|Fecha", next_line, re.I):
                    return f"{first}, {next_line}"[:180]
    return first[:180]


def _extract_receptor(text: str) -> dict[str, str]:
    receptor = {"razon_social": "", "cuit": "", "iva_condicion": ""}
    match = re.search(r"Sr\.\(s\)\s*:\s*(?:\[[^\]]+\]\s*)?(.+?)(?:\s{2,}|$)", text or "", re.I | re.M)
    if match:
        receptor["razon_social"] = match.group(1).strip()[:120]
    else:
        named_match = re.search(r"Apellido\s+y\s+Nombre\s*/\s*Raz[oó]n\s+Social\s*:\s*(.+?)(?:\s{2,}|$)", text or "", re.I | re.M)
        if named_match:
            receptor["razon_social"] = named_match.group(1).strip()[:120]

    iva_cuit_match = re.search(
        r"\bIVA\s*:\s*(.+?)\s{2,}CUIT\s*:\s*(\d{2}[- ]?\d{8}[- ]?\d)",
        text or "",
        re.I | re.M,
    )
    if iva_cuit_match:
        receptor["iva_condicion"] = iva_cuit_match.group(1).strip()[:80]
        digits = re.sub(r"\D", "", iva_cuit_match.group(2))
        receptor["cuit"] = f"{digits[:2]}-{digits[2:10]}-{digits[10:]}" if len(digits) == 11 else digits
    elif receptor["razon_social"]:
        tail = (text or "")[(text or "").find(receptor["razon_social"]) :]
        cuit_match = CUIT_RE.search(tail)
        if cuit_match:
            digits = re.sub(r"\D", "", cuit_match.group(1))
            receptor["cuit"] = f"{digits[:2]}-{digits[2:10]}-{digits[10:]}" if len(digits) == 11 else digits
        condition_match = re.search(r"Condici[oó]n\s+frente\s+al\s+IVA\s*:\s*(.+?)(?:\s{2,}|$)", tail, re.I | re.M)
        if condition_match:
            receptor["iva_condicion"] = condition_match.group(1).strip()[:80]
    return receptor


def _clean_address_part(value: str) -> str:
    if re.match(r"\s*(?:CUIT|Ingresos\s+Brutos|IIBB|Fecha)\s*:", value or "", re.I):
        return ""
    value = re.split(r"\s{2,}(?:CUIT|Ingresos\s+Brutos|IIBB|Fecha)\s*:", value or "", flags=re.I)[0]
    value = re.sub(r"\s+", " ", value).strip(" ,")
    return value


def _dict_to_xml(parent: ET.Element, value: Any) -> None:
    if isinstance(value, dict):
        for key, child_value in value.items():
            child = ET.SubElement(parent, key)
            _dict_to_xml(child, child_value)
    elif isinstance(value, list):
        for item in value:
            child = ET.SubElement(parent, "item")
            _dict_to_xml(child, item)
    elif value is None:
        parent.text = ""
    elif isinstance(value, bool):
        parent.text = "true" if value else "false"
    elif isinstance(value, float):
        parent.text = f"{value:.2f}"
    else:
        parent.text = str(value)


def write_debug_text_files(
    *,
    invoice: dict[str, Any],
    output_dir: str | os.PathLike[str],
    pdf_text: str = "",
    ocr_text: str = "",
    combined_text: str = "",
) -> dict[str, str]:
    enabled = os.environ.get("INVOICE_WRITE_DEBUG_TEXTS", "false").lower() == "true"
    if not enabled:
        return {}

    sha256 = invoice.get("origen", {}).get("sha256") or ""
    short_sha = sha256[:8]
    date_stamp = datetime.now().strftime("%Y%m%d")
    base_name = f"FACTURA_{date_stamp}_{short_sha}"

    debug_dir = Path(output_dir) / "debug"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"no se pudo crear directorio debug {debug_dir}: {exc}", flush=True)
        return {}

    files: dict[str, str] = {}

    text_files = {
        "pdf_text": pdf_text,
        "ocr_text": ocr_text,
        "combined_text": combined_text,
    }
    for key, content in text_files.items():
        if not content:
            continue
        path = debug_dir / f"{base_name}_{key}.txt"
        try:
            _atomic_text_write(path, content)
            files[key] = str(path)
        except OSError as exc:
            print(f"no se pudo escribir debug {key}: {exc}", flush=True)

    diag = invoice.get("diagnostico") or build_diagnostico(invoice)
    diag_path = debug_dir / f"{base_name}_diagnostico.json"
    try:
        _atomic_text_write(diag_path, json.dumps(diag, ensure_ascii=False, indent=2))
        files["diagnostico"] = str(diag_path)
    except OSError as exc:
        print(f"no se pudo escribir debug diagnostico: {exc}", flush=True)

    qr_afip = invoice.get("qr_afip") or {}
    if qr_afip.get("detectado"):
        qr_path = debug_dir / f"{base_name}_qr.json"
        try:
            _atomic_text_write(qr_path, json.dumps(qr_afip, ensure_ascii=False, indent=2))
            files["qr"] = str(qr_path)
        except OSError as exc:
            print(f"no se pudo escribir debug qr: {exc}", flush=True)

    if files:
        invoice.setdefault("diagnostico", {}).setdefault("debug_files", {}).update(files)
        invoice.setdefault("origen", {})["debug_files"] = dict(files)

    return files


def _atomic_text_write(path: Path, text: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _atomic_binary_write(path: Path, data: bytes) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(path)


def _atomic_copy(src: Path, dest: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name, suffix=".tmp", dir=str(dest.parent))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp_name)
        Path(tmp_name).replace(dest)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
