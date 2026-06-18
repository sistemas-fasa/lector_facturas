"""Tests for the internal read-only invoice query API (GET endpoints)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from io import BytesIO
from urllib.parse import urlparse

import pytest

from host_invoice_parser_service import (
    _invoice_list_item,
    _scan_invoice_files,
    _compute_invoices_summary,
    _summarize_job,
    OUTPUT_DIR,
)


SAMPLE_INVOICE = {
    "version": "1.0",
    "estado": "OK",
    "fecha_proceso": "2026-06-18T20:30:08+00:00",
    "origen": {
        "tipo": "email",
        "archivo_original": "factura_BAW.pdf",
        "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "email": {
            "from": "proveedor@baw.com.ar",
            "subject": "FE BAW ELECTRIC",
            "date": "2026-06-18T16:36:53-0300",
        },
        "clasificacion_documento": {"tipo_documento": "FACTURA", "confianza": 90},
    },
    "comprobante": {
        "tipo": "FACTURA",
        "letra": "A",
        "punto_venta": "00005",
        "numero": "00237862",
        "fecha_emision": "2026-06-18",
        "moneda": "ARS",
        "cae": "86250254081471",
    },
    "emisor": {
        "razon_social": "BAW ELECTRIC S.A.",
        "cuit": "30-66180083-2",
    },
    "importes": {
        "neto_gravado": 800000.00,
        "iva_21": 168000.00,
        "total": 968000.00,
    },
    "qr_afip": {"detectado": True, "url": "https://example.com/qr"},
    "validaciones": {
        "requiere_revision": False,
        "observaciones": [],
    },
    "diagnostico": {
        "recomendacion": "ok",
        "clasificacion_documento": {"tipo_documento": "FACTURA"},
    },
}

SAMPLE_INVOICE_REVIEW = {
    "version": "1.0",
    "estado": "REVIEW_REQUIRED",
    "fecha_proceso": "2026-06-18T19:44:55+00:00",
    "origen": {
        "tipo": "email",
        "archivo_original": "ilegible.pdf",
        "sha256": "bbbbbb22222222bbbbbb22222222bbbbbb22222222bbbbbb22222222bbbbbb22",
        "email": {},
        "clasificacion_documento": {"tipo_documento": "ILEGIBLE", "confianza": 0},
    },
    "comprobante": {
        "tipo": "FACTURA", "letra": "", "punto_venta": "", "numero": "",
        "fecha_emision": None, "moneda": "ARS", "cae": None,
    },
    "emisor": {"razon_social": "", "cuit": ""},
    "importes": {"neto_gravado": 0, "iva_21": 0, "total": 0},
    "qr_afip": {},
    "validaciones": {
        "requiere_revision": True,
        "observaciones": ["no se pudo extraer texto del archivo"],
    },
    "diagnostico": {
        "recomendacion": "reintentar",
        "clasificacion_documento": {"tipo_documento": "ILEGIBLE"},
    },
}

SAMPLE_INVOICE_NC = {
    "version": "1.0",
    "estado": "OK",
    "fecha_proceso": "2026-06-18T19:37:37+00:00",
    "origen": {
        "tipo": "email",
        "archivo_original": "1600529275.PDF",
        "sha256": "cccccc33333333cccccc33333333cccccc33333333cccccc33333333cccccc33",
        "email": {"from": "compras@ferreteriaavenida.com.ar", "subject": "NC ROCA ARGENTINA"},
        "clasificacion_documento": {"tipo_documento": "FACTURA", "confianza": 85},
    },
    "comprobante": {
        "tipo": "NOTA_CREDITO", "letra": "A",
        "punto_venta": "0013", "numero": "00213556",
        "fecha_emision": "2026-06-18", "moneda": "ARS",
        "cae": "86250232703798",
    },
    "emisor": {
        "razon_social": "Roca Argentina S.A.",
        "cuit": "30-50052655-2",
    },
    "importes": {"neto_gravado": 158093.94, "iva_21": 42026.32, "total": 200120.26},
    "qr_afip": {},
    "validaciones": {"requiere_revision": False, "observaciones": []},
    "diagnostico": {
        "recomendacion": "ok",
        "clasificacion_documento": {"tipo_documento": "FACTURA"},
    },
}


@pytest.fixture
def temp_output_dir(tmp_path: Path) -> str:
    originals = tmp_path / "originales"
    originals.mkdir()
    for prefix, data in [
        ("FACTURA_20260618_abcdef12", SAMPLE_INVOICE),
        ("FACTURA_20260618_bbbbbb22", SAMPLE_INVOICE_REVIEW),
        ("FACTURA_20260618_cccccc33", SAMPLE_INVOICE_NC),
    ]:
        (tmp_path / f"{prefix}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp_path / f"{prefix}.xml").write_bytes(b"<xml/>")
        (tmp_path / f"{prefix}.ready").write_bytes(b"")
    return str(tmp_path)


class TestInvoiceListItem:
    def test_ok_invoice(self, tmp_path: Path):
        f = tmp_path / "ok.json"
        f.write_text(json.dumps(SAMPLE_INVOICE, ensure_ascii=False), encoding="utf-8")
        item = _invoice_list_item(str(f))
        assert item is not None
        assert item["estado"] == "OK"
        assert item["emisor_razon_social"] == "BAW ELECTRIC S.A."
        assert item["total"] == 968000.00

    def test_review_required(self, tmp_path: Path):
        f = tmp_path / "review.json"
        f.write_text(json.dumps(SAMPLE_INVOICE_REVIEW, ensure_ascii=False), encoding="utf-8")
        item = _invoice_list_item(str(f))
        assert item is not None
        assert item["requiere_revision"] is True
        assert item["clasificacion_documento"] == "ILEGIBLE"

    def test_invalid_json(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        item = _invoice_list_item(str(bad))
        assert item is None


class TestScanInvoiceFiles:
    def test_list_all(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir)
        assert result["total"] == 3
        assert len(result["items"]) == 3

    def test_limit_offset(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, limit=1, offset=0)
        assert len(result["items"]) == 1
        assert result["total"] == 3

        result2 = _scan_invoice_files(temp_output_dir, limit=1, offset=1)
        assert len(result2["items"]) == 1

    def test_filter_status(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, status_filter="REVIEW_REQUIRED")
        assert result["total"] == 1
        assert result["items"][0]["estado"] == "REVIEW_REQUIRED"

    def test_filter_requires_review(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, requires_review="true")
        assert result["total"] == 1
        assert result["items"][0]["requiere_revision"] is True

    def test_filter_provider_cuit(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, provider_cuit="30-66180083-2")
        assert result["total"] == 1
        assert result["items"][0]["emisor_razon_social"] == "BAW ELECTRIC S.A."

    def test_filter_provider_name(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, provider_name="roca argentina")
        assert result["total"] == 1
        assert "Roca" in result["items"][0]["emisor_razon_social"]

    def test_filter_date_range(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, date_from="2026-06-18", date_to="2026-06-18")
        assert result["total"] == 2  # the review invoice has None fecha_emision

        result2 = _scan_invoice_files(temp_output_dir, date_from="2026-06-19")
        assert result2["total"] == 0

    def test_filter_invoice_type(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, invoice_type="NOTA_CREDITO")
        assert result["total"] == 1

    def test_filter_text_search(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, q="baw")
        assert result["total"] == 1

    def test_filter_no_match(self, temp_output_dir: str):
        result = _scan_invoice_files(temp_output_dir, provider_cuit="99-99999999-9")
        assert result["total"] == 0


class TestComputeInvoicesSummary:
    def test_summary(self, temp_output_dir: str):
        scanned = _scan_invoice_files(temp_output_dir)
        summary = _compute_invoices_summary(scanned["all_matched"])
        assert summary["total"] == 3
        assert summary["ok"] >= 1
        assert summary["review_required"] >= 1
        assert "por_proveedor" in summary
        assert "fallas_frecuentes" in summary

    def test_empty(self):
        summary = _compute_invoices_summary([])
        assert summary["total"] == 0
        assert summary["ok"] == 0
        assert summary["review_required"] == 0


class TestHandlerRoutes:
    @pytest.fixture
    def handler(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        inst.send_json = send_json
        inst.send_error = send_error
        return inst

    def test_health_route(self, handler):
        handler.path = "/health"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert payload["status"] == "OK"

    def test_list_invoices(self, handler):
        handler.path = "/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert "items" in payload
        assert "summary" in payload

    def test_review_route(self, handler):
        handler.path = "/invoices/review"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        for item in payload["items"]:
            assert item["requiere_revision"] is True

    def test_invoice_by_sha(self, handler):
        handler.path = "/invoices/by-sha/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert payload.get("estado") == "OK"

    def test_invoice_by_sha_not_found(self, handler):
        handler.path = "/invoices/by-sha/0000000000000000000000000000000000000000000000000000000000000000"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 404

    def test_queue_status(self, handler, temp_output_dir: str):
        handler.path = "/queue/status"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert "queue_dir" in payload

    def test_queue_job_detail_not_found(self, handler):
        handler.path = "/queue/jobs/doesnotexist"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 404

    def test_404_unknown(self, handler):
        handler.path = "/nonexistent"
        handler.do_GET()
        assert handler.send_error_called == 404

    def test_filtered_list(self, handler):
        handler.path = "/invoices?status=REVIEW_REQUIRED"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert payload["total"] == 1
        assert payload["items"][0]["estado"] == "REVIEW_REQUIRED"

    def test_queue_jobs_list(self, handler):
        handler.path = "/queue/jobs"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert "items" in payload
        assert "total" in payload

    def test_detail_omits_ocr_text(self, handler):
        handler.path = "/invoices/1"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert payload.get("ocr_text_omitted") is True

    def test_detail_not_found(self, handler):
        handler.path = "/invoices/99999"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 404

    def test_non_numeric_id_returns_400(self, handler):
        handler.path = "/invoices/abc"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 400
        assert "numerico" in str(payload.get("error", ""))

    def test_invalid_sha_returns_400(self, handler):
        handler.path = "/invoices/by-sha/not-a-valid-sha"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 400
        assert "SHA256" in str(payload.get("error", ""))


class TestAdminAuthorization:
    @pytest.fixture
    def handler(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        inst.send_html_called = None
        inst.headers = {}
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        def serve_html(html_content, status=200):
            inst.send_html_called = (html_content, status)
        inst.send_json = send_json
        inst.send_error = send_error
        inst._serve_html = serve_html
        return inst

    def test_no_token_allows_access(self, handler):
        handler.path = "/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 200

    def test_token_set_blocks_unauthorized(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.path = "/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 401
        assert "Unauthorized" in str(payload.get("error", ""))

    def test_bearer_token_allows_access(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.headers = {"authorization": "Bearer secret123"}
        handler.path = "/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 200

    def test_query_token_param_allows_access(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.path = "/invoices?token=secret123"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 200

    def test_health_exempt_from_auth(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.path = "/health"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert payload["status"] == "OK"
        assert status == 200

    def test_invalid_token_returns_401(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.headers = {"authorization": "Bearer wrongtoken"}
        handler.path = "/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 401

    def test_admin_html_route_blocked_without_token(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.path = "/admin/invoices"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 401

    def test_admin_html_route_allowed_with_token(self, handler, monkeypatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
        handler.headers = {"authorization": "Bearer secret123"}
        handler.path = "/admin/invoices"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 200


class TestAdminHTML:
    @pytest.fixture
    def handler(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        inst.send_html_called = None
        inst.headers = {}
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        def serve_html(html_content, status=200):
            inst.send_html_called = (html_content, status)
        inst.send_json = send_json
        inst.send_error = send_error
        inst._serve_html = serve_html
        return inst

    def test_admin_list_returns_html(self, handler):
        handler.path = "/admin/invoices"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 200
        assert "<html" in html_content
        assert "Facturas procesadas" in html_content

    def test_admin_list_shows_summary(self, handler):
        handler.path = "/admin/invoices"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "Total" in html_content
        assert "OK" in html_content
        assert "Revision" in html_content

    def test_admin_list_shows_invoice_rows(self, handler):
        handler.path = "/admin/invoices"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "BAW ELECTRIC" in html_content
        assert "Roca Argentina" in html_content

    def test_admin_list_has_detail_links(self, handler):
        handler.path = "/admin/invoices"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "/admin/invoices/" in html_content
        assert "ver" in html_content

    def test_admin_list_has_api_links(self, handler):
        handler.path = "/admin/invoices"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "/invoices" in html_content
        assert "/queue/status" in html_content

    def test_admin_list_handles_special_chars_in_query(self, handler):
        handler.path = "/admin/invoices?q=<script>alert(1)</script>"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert status == 200
        assert "No se encontraron" in html_content

    def test_admin_detail_returns_html(self, handler):
        handler.path = "/admin/invoices/1"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 200
        assert "<html" in html_content
        assert "Factura" in html_content

    def test_admin_detail_shows_invoice_data(self, handler):
        handler.path = "/admin/invoices/1"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "Roca Argentina" in html_content
        assert "200,120.26" in html_content or "200.120" in html_content

    def test_admin_detail_has_back_link(self, handler):
        handler.path = "/admin/invoices/1"
        handler.do_GET()
        html_content, status = handler.send_html_called
        assert "/admin/invoices" in html_content

    def test_admin_detail_not_found(self, handler):
        handler.path = "/admin/invoices/99999"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 404
        assert "no encontrada" in html_content.lower()

    def test_admin_detail_escapes_html_in_data(self, handler, tmp_path):
        malicious = dict(SAMPLE_INVOICE)
        malicious["emisor"]["razon_social"] = "<script>alert('xss')</script>"
        f = tmp_path / "FACTURA_20260618_zzzzzz99.json"
        f.write_text(json.dumps(malicious, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "FACTURA_20260618_zzzzzz99.xml").write_bytes(b"<xml/>")
        handler.path = "/admin/invoices/1"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert "<script>" not in html_content
        assert "&lt;script&gt;" in html_content or "&#x3C;script&#x3E;" in html_content

    def test_admin_detail_non_numeric_id_returns_error(self, handler):
        handler.path = "/admin/invoices/abc"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 400
        assert "ID invalido" in html_content or "numerico" in html_content
        assert "Volver" in html_content


class TestQueueJobsEndpoint:
    @pytest.fixture
    def handler(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        monkeypatch.setattr("host_invoice_parser_service.QUEUE_DIR", Path(temp_output_dir) / "cola")
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        inst.headers = {}
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        inst.send_json = send_json
        inst.send_error = send_error
        return inst

    def test_job_detail_path_traversal_blocked(self, handler):
        handler.path = "/queue/jobs/../etc/passwd"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 400
        assert "invalido" in str(payload.get("error", ""))

    def test_job_detail_empty_response(self, handler):
        handler.path = "/queue/jobs/slq_12345"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 404

    def test_jobs_list_omits_internal_paths(self, handler, temp_output_dir: str):
        pendientes = Path(temp_output_dir) / "cola" / "pendientes"
        pendientes.mkdir(parents=True)
        job = {
            "job_id": "12345-test",
            "status": "QUEUED",
            "original_filename": "factura.pdf",
            "sha256": "abcd1234",
            "source_type": "email",
            "original_path": "/secret/path/original.pdf",
            "metadata_path": "/secret/path/meta.json",
            "source_metadata": {"email": {"from": "test@test.com"}},
            "queued_at": "2026-06-18T12:00:00",
        }
        (pendientes / "12345-test.json").write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8"
        )
        handler.path = "/queue/jobs"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 200
        assert len(payload["items"]) >= 1
        item = next(i for i in payload["items"] if i.get("job_id") == "12345-test")
        assert "original_path" not in item
        assert "metadata_path" not in item
        assert item.get("original_filename") == "factura.pdf"
        assert item.get("sha256") == "abcd1234"


class TestSummarizeJob:
    def test_keeps_safe_fields(self):
        job = {
            "job_id": "abc123",
            "status": "DONE",
            "sha256": "abcd",
            "source_type": "email",
            "original_filename": "inv.pdf",
            "original_path": "/should/not/appear",
            "metadata_path": "/should/not/appear",
            "source_metadata": {"email": {"from": "x@x.com"}},
            "queued_at": "2026-01-01T00:00:00",
            "started_at": "2026-01-01T00:01:00",
            "finished_at": "2026-01-01T00:02:00",
            "force": False,
            "result": {
                "status": "OK",
                "requires_review": False,
                "staging": {"ok": True},
                "json_file": "/internal/path/file.json",
            },
        }
        result = _summarize_job(job)
        assert result["job_id"] == "abc123"
        assert result["status"] == "DONE"
        assert result["original_filename"] == "inv.pdf"
        assert "original_path" not in result
        assert "metadata_path" not in result
        assert "source_metadata" not in result
        assert result["result"]["status"] == "OK"
        assert result["result"]["requires_review"] is False
        assert result["result"]["staging"] == {"ok": True}
        assert "json_file" not in result["result"]

    def test_handles_unreadable_job(self):
        job = {"job_file": "/some/path.json", "_queue_dir": "pendientes", "status": "UNREADABLE"}
        result = _summarize_job(job)
        assert result["status"] == "UNREADABLE"
        assert result["job_file"] == "/some/path.json"

    def test_handles_minimal_job(self):
        job = {"job_id": "minimal"}
        result = _summarize_job(job)
        assert result["job_id"] == "minimal"
        assert "original_path" not in result


class TestSummarizeDetail:
    def test_ocr_omitted_flag(self, tmp_path):
        data = dict(SAMPLE_INVOICE)
        data["ocr"] = {"texto": "CONFIDENTIAL OCR TEXT " * 100}
        from host_invoice_parser_service import _summarize_detail
        result = _summarize_detail(data)
        assert result.get("ocr_text_omitted") is True
        assert "ocr_text" not in result
        assert result.get("ocr_chars", 0) > 0

    def test_no_ocr_field(self, tmp_path):
        data = dict(SAMPLE_INVOICE)
        if "ocr" in data:
            del data["ocr"]
        from host_invoice_parser_service import _summarize_detail
        result = _summarize_detail(data)
        assert result.get("ocr_text_omitted") is True
        assert result.get("ocr_chars", 0) == 0

    def test_minimal_invoice(self, tmp_path):
        data = {"estado": "OK", "origen": {}, "validaciones": {}, "diagnostico": {}}
        from host_invoice_parser_service import _summarize_detail
        result = _summarize_detail(data)
        assert result.get("estado") == "OK"
        assert result.get("ocr_text_omitted") is True

    def test_list_item_with_fallas(self, tmp_path):
        data = dict(SAMPLE_INVOICE)
        data["validaciones"]["observaciones"] = ["error 1", "error 2"]
        from host_invoice_parser_service import _invoice_list_item
        f = tmp_path / "FACTURA_20260618_test001.json"
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        item = _invoice_list_item(str(f))
        assert item is not None
        assert len(item["fallas_principales"]) == 2
