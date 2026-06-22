from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class AiValidationResult:
    ok: bool
    normalized: dict[str, Any]
    failures: list[dict[str, Any]]


def _digits(value: Any) -> str | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits or None


def _amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    raw = str(value).strip().replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        year, month, day = map(int, iso.groups())
    else:
        local = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw)
        if not local:
            return None
        day, month, year = map(int, local.groups())
        if year < 100:
            year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _format_point(value: Any) -> str | None:
    digits = _digits(value)
    if not digits or int(digits) <= 0:
        return None
    return digits.zfill(4)


def _format_number(value: Any) -> str | None:
    digits = _digits(value)
    if not digits or int(digits) <= 0:
        return None
    return digits.zfill(8)


def _failure(code: str, campo: str, detalle: str) -> dict[str, Any]:
    return {"codigo": code, "campo": campo, "detalle": detalle}


def normalize_ai_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload or {})
    proveedor = normalized.setdefault("proveedor", {})
    comprobante = normalized.setdefault("comprobante", {})
    cae = normalized.setdefault("cae", {})
    importes = normalized.setdefault("importes", {})
    confianza = normalized.setdefault("confianza", {})

    proveedor["razon_social"] = (str(proveedor.get("razon_social")).strip() if proveedor.get("razon_social") not in (None, "") else None)
    proveedor["cuit"] = _digits(proveedor.get("cuit"))

    comprobante["tipo"] = (str(comprobante.get("tipo")).strip().upper() if comprobante.get("tipo") not in (None, "") else None)
    comprobante["letra"] = (str(comprobante.get("letra")).strip().upper()[:1] if comprobante.get("letra") not in (None, "") else None)
    code = _digits(comprobante.get("codigo_afip"))
    comprobante["codigo_afip"] = code.zfill(3) if code else None
    comprobante["punto_venta"] = _format_point(comprobante.get("punto_venta"))
    comprobante["numero"] = _format_number(comprobante.get("numero"))
    comprobante["fecha_emision"] = _date(comprobante.get("fecha_emision"))

    cae["numero"] = _digits(cae.get("numero"))
    cae["vencimiento"] = _date(cae.get("vencimiento"))

    for name in (
        "neto_gravado",
        "iva_21",
        "iva_105",
        "iva_27",
        "exento",
        "no_gravado",
        "percepciones",
        "percepciones_iibb",
        "otros_impuestos",
        "total",
    ):
        importes[name] = _amount(importes.get(name))

    moneda = normalized.get("moneda")
    normalized["moneda"] = str(moneda).strip().upper() if moneda not in (None, "") else "ARS"
    confianza["general"] = _amount(confianza.get("general")) or 0.0
    campos_dudosos = confianza.get("campos_dudosos")
    confianza["campos_dudosos"] = campos_dudosos if isinstance(campos_dudosos, list) else []
    observaciones = normalized.get("observaciones")
    normalized["observaciones"] = observaciones if isinstance(observaciones, list) else []
    return normalized


def validate_ai_invoice(
    payload: dict[str, Any],
    *,
    min_confidence: float,
    total_tolerance: float,
    require_critical_fields: bool = True,
) -> AiValidationResult:
    normalized = normalize_ai_payload(payload)
    failures: list[dict[str, Any]] = []
    proveedor = normalized["proveedor"]
    comprobante = normalized["comprobante"]
    cae = normalized["cae"]
    importes = normalized["importes"]
    confianza = normalized["confianza"]

    if not (proveedor.get("cuit") or proveedor.get("razon_social")):
        failures.append(_failure("MISSING_CRITICAL_FIELD", "proveedor", "Falta CUIT o razon social del proveedor"))
    if not (comprobante.get("tipo") or comprobante.get("letra") or comprobante.get("codigo_afip")):
        failures.append(_failure("MISSING_CRITICAL_FIELD", "tipo_comprobante", "Falta tipo/letra/codigo AFIP"))
    if not comprobante.get("punto_venta"):
        failures.append(_failure("INVALID_POINT_OF_SALE", "punto_venta", "Punto de venta ausente, cero o invalido"))
    if not comprobante.get("numero"):
        failures.append(_failure("INVALID_NUMBER", "numero", "Numero ausente, cero o invalido"))
    if not comprobante.get("fecha_emision"):
        failures.append(_failure("INVALID_DATE", "fecha_emision", "Fecha de emision ausente o invalida"))
    total = importes.get("total")
    if total is None or total <= 0:
        failures.append(_failure("INVALID_TOTAL", "total", "Total ausente, no numerico o cero"))

    cuit = proveedor.get("cuit")
    if cuit and len(cuit) != 11:
        failures.append(_failure("INVALID_CUIT", "proveedor.cuit", "CUIT debe tener 11 digitos"))

    cae_numero = cae.get("numero")
    if cae_numero and not (10 <= len(cae_numero) <= 20):
        failures.append(_failure("INVALID_CAE", "cae.numero", "CAE con longitud no razonable"))

    confidence = float(confianza.get("general") or 0)
    if confidence < min_confidence:
        failures.append(_failure("LOW_CONFIDENCE", "confianza.general", "Confianza general por debajo del minimo"))

    if require_critical_fields:
        doubtful = {str(item) for item in confianza.get("campos_dudosos") or []}
        for field_name in ("proveedor", "tipo_comprobante", "punto_venta", "numero", "fecha_emision", "total"):
            if field_name in doubtful:
                failures.append(_failure("DOUBTFUL_CRITICAL_FIELD", field_name, "Campo critico marcado como dudoso"))

    component_names = [
        "neto_gravado",
        "iva_21",
        "iva_105",
        "iva_27",
        "percepciones",
        "percepciones_iibb",
        "otros_impuestos",
        "exento",
        "no_gravado",
    ]
    components = [importes.get(name) for name in component_names if importes.get(name) is not None]
    if total is not None and components:
        calculated = round(sum(float(value) for value in components), 2)
        if abs(calculated - float(total)) > float(total_tolerance):
            failures.append(
                {
                    "codigo": "TOTAL_MISMATCH",
                    "campo": "total",
                    "detalle": f"Suma de componentes {calculated:.2f} difiere de total {float(total):.2f}",
                }
            )

    return AiValidationResult(ok=not failures, normalized=normalized, failures=failures)
