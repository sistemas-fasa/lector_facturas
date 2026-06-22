from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from .openrouter_client import OpenRouterClient
from .schema import AiExtractorConfig
from .validators import AiValidationResult, validate_ai_invoice


LegacyExtractor = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class AiFirstResult:
    invoice: dict[str, Any]
    trace: dict[str, Any]


def _empty_trace(config: AiExtractorConfig) -> dict[str, Any]:
    return {
        "ai": {
            "provider": config.provider,
            "model": config.model,
            "fallback_model_used": False,
            "enabled": config.enabled,
            "confidence": 0.0,
            "campos_dudosos": [],
            "observaciones": [],
            "raw_response": {},
            "error": config.config_error,
            "validaciones": {"ok": False, "fallas": []},
        },
        "fallback_usado": False,
        "fallback_tipo": None,
        "validaciones": {"ok": False, "fallas": []},
    }


def extract_invoice_ai_first(
    *,
    document_bytes: bytes,
    filename: str,
    mime_type: str,
    sha256: str,
    legacy_extractor: LegacyExtractor,
    client: Any | None = None,
    config: AiExtractorConfig | None = None,
) -> AiFirstResult:
    cfg = config or AiExtractorConfig.from_env()
    trace = _empty_trace(cfg)

    if not cfg.enabled or cfg.config_error:
        return _run_legacy(trace, legacy_extractor)

    openrouter = client or OpenRouterClient(api_key=cfg.api_key)
    models = [cfg.model]
    if cfg.fallback_model:
        models.append(cfg.fallback_model)

    last_error = None
    for idx, model in enumerate(models):
        for attempt in range(cfg.max_retries + 1):
            try:
                raw = openrouter.extract_invoice(
                    document_bytes=document_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    model=model,
                    timeout_seconds=cfg.timeout_seconds,
                )
                payload = _parse_json(raw)
                validation = validate_ai_invoice(
                    payload,
                    min_confidence=cfg.min_confidence,
                    total_tolerance=cfg.total_tolerance,
                    require_critical_fields=cfg.require_critical_fields,
                )
                trace["ai"].update(_trace_from_validation(validation, cfg, raw, model, fallback_model_used=idx > 0))
                trace["validaciones"] = trace["ai"]["validaciones"]
                if validation.ok:
                    return AiFirstResult(invoice=_invoice_from_ai(validation, sha256, filename, mime_type, trace), trace=trace)
                return _run_legacy(trace, legacy_extractor)
            except json.JSONDecodeError:
                trace["ai"]["error"] = "invalid_json"
                last_error = "invalid_json"
                break
            except TimeoutError:
                trace["ai"]["error"] = "timeout"
                last_error = "timeout"
                if attempt < cfg.max_retries:
                    continue
                break
            except Exception as exc:
                trace["ai"]["error"] = _classify_error(exc)
                last_error = trace["ai"]["error"]
                if attempt < cfg.max_retries:
                    continue
                break
        if idx + 1 < len(models) and last_error not in {"invalid_json", "timeout"}:
            continue
        if idx + 1 < len(models) and last_error in {"timeout"}:
            continue
        if idx + 1 < len(models) and last_error == "invalid_json":
            continue

    if last_error and trace["ai"]["error"] is None:
        trace["ai"]["error"] = last_error
    return _run_legacy(trace, legacy_extractor)


def _parse_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("AI response is not an object", text, 0)
    return data


def _trace_from_validation(
    validation: AiValidationResult,
    config: AiExtractorConfig,
    raw: str,
    model: str,
    *,
    fallback_model_used: bool,
) -> dict[str, Any]:
    normalized = validation.normalized
    confianza = normalized.get("confianza") or {}
    return {
        "provider": config.provider,
        "model": model,
        "fallback_model_used": fallback_model_used,
        "enabled": True,
        "confidence": float(confianza.get("general") or 0),
        "campos_dudosos": list(confianza.get("campos_dudosos") or []),
        "observaciones": list(normalized.get("observaciones") or []),
        "raw_response": normalized if config.store_raw_response else {},
        "error": None,
        "validaciones": {"ok": validation.ok, "fallas": validation.failures},
    }


def _invoice_from_ai(
    validation: AiValidationResult,
    sha256: str,
    filename: str,
    mime_type: str,
    trace: dict[str, Any],
) -> dict[str, Any]:
    data = validation.normalized
    comprobante = data["comprobante"]
    importes = data["importes"]
    proveedor = data["proveedor"]
    cae = data["cae"]
    invoice = {
        "version": "2.0-ai",
        "estado": "OK" if validation.ok else "REVIEW_REQUIRED",
        "origen": {
            "tipo": "ai_openrouter",
            "archivo_original": filename,
            "mime_type": mime_type,
            "sha256": sha256,
            "phash": "",
            "duplicado": False,
            "motivo_duplicado": None,
            "email": {},
        },
        "comprobante": {
            "tipo": comprobante.get("tipo") or "",
            "codigo": comprobante.get("codigo_afip") or "",
            "letra": comprobante.get("letra") or "",
            "punto_venta": comprobante.get("punto_venta") or "",
            "numero": comprobante.get("numero") or "",
            "fecha_emision": comprobante.get("fecha_emision"),
            "fecha_vencimiento": None,
            "moneda": data.get("moneda") or "ARS",
            "cae": cae.get("numero") or "",
            "cae_vencimiento": cae.get("vencimiento"),
        },
        "emisor": {
            "razon_social": proveedor.get("razon_social") or "",
            "cuit": proveedor.get("cuit") or "",
            "iva_condicion": "",
            "domicilio": "",
        },
        "receptor": {"razon_social": "", "cuit": "", "iva_condicion": ""},
        "importes": importes,
        "percepciones_iibb_detalle": [],
        "items": [],
        "ocr": {"texto": "", "confianza": None, "motor": "openrouter_ai"},
        "qr_afip": {},
        "extraccion_facturas_ocr": {},
        "contabilidad": {},
        "validaciones": {
            "total_detectado": bool(importes.get("total")),
            "cuit_detectado": bool(proveedor.get("cuit")),
            "fecha_detectada": bool(comprobante.get("fecha_emision")),
            "numero_detectado": bool(comprobante.get("numero")),
            "qr_detectado": False,
            "requiere_revision": not validation.ok,
            "observaciones": [failure["detalle"] for failure in validation.failures],
        },
        "extraccion_enriquecida": trace,
    }
    return invoice


def _run_legacy(trace: dict[str, Any], legacy_extractor: LegacyExtractor) -> AiFirstResult:
    if trace.get("fallback_usado") is False:
        trace["fallback_usado"] = True
        trace["fallback_tipo"] = "legacy"
    invoice = legacy_extractor(trace)
    enriched = invoice.setdefault("extraccion_enriquecida", {})
    enriched["ai"] = trace["ai"]
    enriched["fallback_usado"] = trace["fallback_usado"]
    enriched["fallback_tipo"] = trace["fallback_tipo"]
    if "validaciones" not in enriched:
        enriched["validaciones"] = trace["validaciones"]
    return AiFirstResult(invoice=invoice, trace=trace)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"openrouter_http_{exc.code}"
    if isinstance(exc, URLError):
        return "openrouter_request_failed"
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    return "openrouter_error"
