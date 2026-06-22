from __future__ import annotations

import json
import os
from datetime import date
from urllib.error import URLError
from urllib.error import HTTPError
from io import BytesIO

import pytest

from invoice_ai_extractor.schema import AiExtractorConfig
from invoice_ai_extractor.openrouter_client import OpenRouterClient
from invoice_ai_extractor.service import extract_invoice_ai_first
from invoice_ai_extractor.validators import validate_ai_invoice


VALID_AI_JSON = {
    "proveedor": {"razon_social": "DAS DACH S.A.", "cuit": "33711969859"},
    "comprobante": {
        "tipo": "FACTURA",
        "letra": "A",
        "codigo_afip": "001",
        "punto_venta": "0007",
        "numero": "00007777",
        "fecha_emision": "2026-06-19",
    },
    "cae": {"numero": "86250381245054", "vencimiento": "2026-06-29"},
    "importes": {
        "neto_gravado": 11104645.01,
        "iva_21": 2331975.45,
        "iva_105": None,
        "iva_27": None,
        "exento": None,
        "no_gravado": None,
        "percepciones": None,
        "percepciones_iibb": None,
        "otros_impuestos": None,
        "total": 13436620.46,
    },
    "moneda": "ARS",
    "confianza": {"general": 0.92, "campos_dudosos": []},
    "observaciones": [],
}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def extract_invoice(self, *, document_bytes, filename, mime_type, model, timeout_seconds):
        self.calls.append(
            {
                "document_bytes": document_bytes,
                "filename": filename,
                "mime_type": mime_type,
                "model": model,
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def legacy_invoice(status: str = "OK") -> dict:
    return {
        "estado": status,
        "origen": {"sha256": "abc"},
        "comprobante": {"tipo": "FACTURA", "letra": "A", "punto_venta": "0001", "numero": "00000001"},
        "emisor": {"razon_social": "LEGACY SA", "cuit": "30-50000007-0"},
        "importes": {"total": 121.0, "neto_gravado": 100.0, "iva_21": 21.0},
        "validaciones": {"requiere_revision": status != "OK", "observaciones": []},
        "extraccion_enriquecida": {"validaciones": {"ok": status == "OK", "fallas": []}},
    }


def test_ai_disabled_does_not_call_openrouter_and_uses_legacy(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "false")
    client = FakeClient([json.dumps(VALID_AI_JSON)])
    legacy_calls = []

    result = extract_invoice_ai_first(
        document_bytes=b"pdf",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_calls.append(trace) or legacy_invoice(),
        client=client,
    )

    assert client.calls == []
    assert len(legacy_calls) == 1
    assert result.invoice["emisor"]["razon_social"] == "LEGACY SA"
    assert result.trace["ai"]["enabled"] is False
    assert result.trace["fallback_usado"] is True


def test_free_model_allowed(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("INVOICE_AI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    monkeypatch.setenv("INVOICE_AI_ALLOW_FREE_MODELS", "true")
    config = AiExtractorConfig.from_env(os.environ)

    assert config.enabled is True
    assert config.config_error is None


def test_free_model_rejected_when_not_allowed(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("INVOICE_AI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    monkeypatch.setenv("INVOICE_AI_ALLOW_FREE_MODELS", "false")
    client = FakeClient([json.dumps(VALID_AI_JSON)])

    result = extract_invoice_ai_first(
        document_bytes=b"pdf",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_invoice(),
        client=client,
    )

    assert client.calls == []
    assert result.trace["ai"]["error"] == "free_model_not_allowed"
    assert result.trace["fallback_usado"] is True


def test_openrouter_client_sends_api_key_in_authorization_header(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    OpenRouterClient(api_key="test-openrouter-key").extract_invoice(
        document_bytes=b"factura",
        filename="factura.png",
        mime_type="image/png",
        model="openrouter/free",
        timeout_seconds=17,
    )

    assert captured["authorization"] == "Bearer test-openrouter-key"
    assert captured["timeout"] == 17


def test_valid_openrouter_json_builds_ai_invoice(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    client = FakeClient([json.dumps(VALID_AI_JSON)])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: pytest.fail("legacy should not run"),
        client=client,
    )

    assert result.invoice["estado"] == "OK"
    assert result.invoice["emisor"]["razon_social"] == "DAS DACH S.A."
    assert result.invoice["comprobante"]["punto_venta"] == "0007"
    assert result.invoice["importes"]["iva_21"] == 2331975.45
    assert result.trace["fallback_usado"] is False
    assert result.trace["ai"]["model"] == "google/gemini-2.5-flash-lite"


def test_openrouter_zero_point_of_sale_stays_review_required(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    payload = json.loads(json.dumps(VALID_AI_JSON))
    payload["comprobante"]["punto_venta"] = "0000"
    client = FakeClient([json.dumps(payload)])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_invoice("REVIEW_REQUIRED"),
        client=client,
    )

    assert result.invoice["estado"] == "REVIEW_REQUIRED"
    assert result.trace["ai"]["validaciones"]["ok"] is False
    assert any(f["campo"] == "punto_venta" for f in result.trace["ai"]["validaciones"]["fallas"])


def test_invalid_openrouter_json_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    client = FakeClient(["not-json"])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_invoice(),
        client=client,
    )

    assert result.invoice["emisor"]["razon_social"] == "LEGACY SA"
    assert result.trace["fallback_usado"] is True
    assert result.trace["ai"]["error"] == "invalid_json"


def test_openrouter_timeout_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    client = FakeClient([TimeoutError("slow")])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_invoice(),
        client=client,
    )

    assert result.invoice["emisor"]["razon_social"] == "LEGACY SA"
    assert result.trace["ai"]["error"] == "timeout"
    assert result.trace["fallback_usado"] is True


def test_openrouter_http_error_keeps_status_in_trace(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    error = HTTPError(
        url="https://openrouter.ai/api/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=BytesIO(b'{"error":{"message":"User not found.","code":401}}'),
    )
    client = FakeClient([error])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: legacy_invoice(),
        client=client,
    )

    assert result.trace["ai"]["error"] == "openrouter_http_401"
    assert result.trace["fallback_usado"] is True


def test_fallback_model_is_used_when_primary_fails(monkeypatch):
    monkeypatch.setenv("INVOICE_AI_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL", "google/gemini-2.5-flash")
    client = FakeClient([URLError("model failed"), json.dumps(VALID_AI_JSON)])

    result = extract_invoice_ai_first(
        document_bytes=b"%PDF",
        filename="factura.pdf",
        mime_type="application/pdf",
        sha256="abc",
        legacy_extractor=lambda trace: pytest.fail("legacy should not run"),
        client=client,
    )

    assert [call["model"] for call in client.calls] == ["google/gemini-2.5-flash-lite", "google/gemini-2.5-flash"]
    assert result.invoice["estado"] == "OK"
    assert result.trace["ai"]["model"] == "google/gemini-2.5-flash"
    assert result.trace["ai"]["fallback_model_used"] is True


def test_total_mismatch_marks_review_required():
    payload = json.loads(json.dumps(VALID_AI_JSON))
    payload["importes"]["total"] = 999.99

    validation = validate_ai_invoice(payload, min_confidence=0.70, total_tolerance=2)

    assert validation.ok is False
    assert any(f["codigo"] == "TOTAL_MISMATCH" for f in validation.failures)
