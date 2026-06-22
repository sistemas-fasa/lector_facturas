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
    process_invoice_upload,
    _invoice_list_item,
    _scan_invoice_files,
    _compute_invoices_summary,
    _summarize_job,
    _resolve_invoice_file,
    _content_type_for_path,
    _file_link_html,
    _debug_links_html,
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


class TestFileViewerHelpers:
    def test_file_link_html_exists(self):
        result = _file_link_html("1", "json", True)
        assert "/invoices/1/files/json" in result
        assert "Ver json" in result or "Ver JSON" in result
        assert "No disponible" not in result

    def test_file_link_html_not_exists(self):
        result = _file_link_html("5", "original", False)
        assert result == "No disponible"

    def test_debug_links_html_empty(self):
        result = _debug_links_html("1", [])
        assert result == "No disponible"

    def test_debug_links_html_with_files(self, tmp_path):
        paths = [
            str(tmp_path / "abcdef12_diagnostico.json"),
            str(tmp_path / "abcdef12_combined_text.txt"),
        ]
        result = _debug_links_html("1", paths)
        assert "/invoices/1/files/debug/diagnostico" in result
        assert "/invoices/1/files/debug/combined-text" in result
        assert "Diagnóstico" in result
        assert "Texto combinado" in result
        assert "No disponible" not in result


class TestResolveInvoiceFile:
    def test_resolve_json(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "json")
        assert result is not None
        assert result.suffix == ".json"
        assert "FACTURA_" in result.name

    def test_resolve_xml(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "xml")
        assert result is not None
        assert result.suffix == ".xml"

    def test_resolve_original_missing(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "original")
        assert result is None

    def test_resolve_original_exists(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        originals = Path(temp_output_dir) / "originales"
        originals.mkdir(exist_ok=True)
        sha = SAMPLE_INVOICE["origen"]["sha256"]
        original_pdf = originals / f"{sha}.pdf"
        original_pdf.write_bytes(b"%PDF-1.4 mock")
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "original")
        assert result is not None
        assert result.suffix == ".pdf"
        assert result.name == f"{sha}.pdf"

    def test_resolve_debug(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        sha8 = SAMPLE_INVOICE["origen"]["sha256"][:8]
        dbg = Path(temp_output_dir) / f"{sha8}_diagnostico.json"
        dbg.write_text('{"ok": true}', encoding="utf-8")
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "debug", "diagnostico")
        assert result is not None
        assert result.name == f"{sha8}_diagnostico.json"

    def test_resolve_debug_not_found(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "debug", "qr")
        assert result is None

    def test_resolve_invalid_type(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        invoice = dict(SAMPLE_INVOICE)
        result = _resolve_invoice_file(invoice, "nonexistent")
        assert result is None

    def test_path_traversal_blocked(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", str(tmp_path))
        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        (safe_dir / "dummy.json").write_text("{}", encoding="utf-8")
        invoice = {
            "origen": {"sha256": "aa"},
            "_staging_id": "99",
        }
        result = _resolve_invoice_file(invoice, "json")
        assert result is None or not str(result).startswith(str(tmp_path / ".."))


class TestContentTypeForPath:
    def test_pdf(self):
        assert _content_type_for_path(Path("x.pdf")) == "application/pdf"

    def test_json(self):
        assert _content_type_for_path(Path("x.json")) == "application/json; charset=utf-8"

    def test_xml(self):
        assert _content_type_for_path(Path("x.xml")) == "application/xml; charset=utf-8"

    def test_png(self):
        assert _content_type_for_path(Path("x.png")) == "image/png"

    def test_jpg(self):
        assert _content_type_for_path(Path("x.jpg")) == "image/jpeg"

    def test_unknown(self):
        assert _content_type_for_path(Path("x.xyz")) == "application/octet-stream"


class TestFileViewerEndpoints:
    @pytest.fixture
    def handler_with_files(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        originals = Path(temp_output_dir) / "originales"
        originals.mkdir(exist_ok=True)
        # Create original files for all sample invoices
        for sample in (SAMPLE_INVOICE, SAMPLE_INVOICE_REVIEW, SAMPLE_INVOICE_NC):
            sha = sample["origen"]["sha256"]
            (originals / f"{sha}.pdf").write_bytes(b"%PDF-1.4 invoice data")
            sha8 = sha[:8]
            (Path(temp_output_dir) / f"{sha8}_diagnostico.json").write_text(
                '{"ok": true}', encoding="utf-8"
            )
            (Path(temp_output_dir) / f"{sha8}_combined_text.txt").write_text(
                "linea 1\nlinea 2", encoding="utf-8"
            )
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        inst.headers = {}
        inst._response_status = None
        inst._response_headers = []
        inst._response_data = None
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        def send_response(status):
            inst._response_status = status
        def send_header(k, v):
            inst._response_headers.append((k, v))
        def end_headers():
            pass
        inst.send_json = send_json
        inst.send_error = send_error
        inst.send_response = send_response
        inst.send_header = send_header
        inst.end_headers = end_headers
        inst.wfile = BytesIO()
        return inst

    def test_serve_original_by_id(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/1/files/original"
        handler.do_GET()
        assert handler.send_json_called is None
        assert handler._response_status == 200
        ctype = dict(handler._response_headers).get("Content-Type", "")
        assert "pdf" in ctype.lower()

    def test_serve_json_by_id(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/1/files/json"
        handler.do_GET()
        assert handler.send_json_called is None
        assert handler._response_status == 200
        ctype = dict(handler._response_headers).get("Content-Type", "")
        assert "json" in ctype.lower()

    def test_serve_xml_by_id(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/1/files/xml"
        handler.do_GET()
        assert handler.send_json_called is None
        assert handler._response_status == 200

    def test_serve_debug_by_id(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/1/files/debug/diagnostico"
        handler.do_GET()
        assert handler.send_json_called is None
        assert handler._response_status == 200

    def test_correct_content_type_png(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        originals = Path(temp_output_dir) / "originales"
        originals.mkdir(exist_ok=True)
        # ID 3 = SAMPLE_INVOICE (abcdef12...) after descending sort
        sha = SAMPLE_INVOICE["origen"]["sha256"]
        (originals / f"{sha}.pdf").unlink(missing_ok=True)
        (originals / f"{sha}.png").write_bytes(b"\x89PNG mock")
        from host_invoice_parser_service import Handler
        inst = Handler.__new__(Handler)
        inst.server_version = "Test/1.0"
        inst.send_json_called = None
        inst.send_error_called = None
        inst.headers = {}
        inst._response_status = None
        inst._response_headers = []
        def send_json(payload, status=200):
            inst.send_json_called = (payload, status)
        def send_error(status):
            inst.send_error_called = status
        def send_response(status):
            inst._response_status = status
        def send_header(k, v):
            inst._response_headers.append((k, v))
        def end_headers():
            pass
        inst.send_json = send_json
        inst.send_error = send_error
        inst.send_response = send_response
        inst.send_header = send_header
        inst.end_headers = end_headers
        inst.wfile = BytesIO()
        inst.path = "/invoices/3/files/original"
        inst.do_GET()
        assert inst.send_json_called is None
        assert inst._response_status == 200
        ctype = dict(inst._response_headers).get("Content-Type", "")
        assert "image/png" in ctype.lower()

    def test_serve_file_404(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/99999/files/original"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 404

    def test_serve_file_invalid_id_returns_400(self, handler_with_files):
        handler = handler_with_files
        handler.path = "/invoices/abc/files/json"
        handler.do_GET()
        assert handler.send_json_called is not None
        payload, status = handler.send_json_called
        assert status == 400
        assert "numerico" in str(payload.get("error", "")).lower()

    def test_file_endpoints_require_auth(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
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
        inst.path = "/invoices/1/files/json"
        inst.do_GET()
        assert inst.send_json_called is not None
        payload, status = inst.send_json_called
        assert status == 401

    def test_health_exempt_from_auth_on_file_routes(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.ADMIN_TOKEN", "secret123")
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
        inst.path = "/health"
        inst.do_GET()
        assert inst.send_json_called is not None
        payload, status = inst.send_json_called
        assert status == 200
        assert payload["status"] == "OK"


class TestAdminHTMLFileLinks:
    @pytest.fixture
    def handler(self, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("host_invoice_parser_service.OUTPUT_DIR", temp_output_dir)
        originals = Path(temp_output_dir) / "originales"
        originals.mkdir(exist_ok=True)
        # Only create files for SAMPLE_INVOICE_NC (ID 1 after descending sort)
        sha = SAMPLE_INVOICE_NC["origen"]["sha256"]
        (originals / f"{sha}.pdf").write_bytes(b"%PDF mock")
        sha8 = sha[:8]
        (Path(temp_output_dir) / f"{sha8}_diagnostico.json").write_text(
            '{"ok": true}', encoding="utf-8"
        )
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

    def test_admin_detail_shows_file_links_when_files_exist(self, handler):
        handler.path = "/admin/invoices/1"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 200
        assert "/invoices/1/files/json" in html_content
        assert "/invoices/1/files/xml" in html_content
        assert "/invoices/1/files/original" in html_content
        assert "/invoices/1/files/debug/diagnostico" in html_content
        assert "Ver JSON" in html_content or "Ver json" in html_content
        assert "No disponible" not in html_content

    def test_admin_detail_shows_no_links_when_files_missing(self, handler):
        handler.path = "/admin/invoices/2"
        handler.do_GET()
        assert handler.send_html_called is not None
        html_content, status = handler.send_html_called
        assert status == 200
        assert "No disponible" in html_content


def test_process_invoice_upload_ai_success_does_not_call_legacy_extractors(monkeypatch, tmp_path: Path):
    import host_invoice_parser_service as service

    invoice = {
        "version": "2.0-ai",
        "estado": "OK",
        "origen": {
            "tipo": "ai_openrouter",
            "archivo_original": "factura.pdf",
            "mime_type": "application/pdf",
            "sha256": "abc",
        },
        "comprobante": {
            "tipo": "FACTURA",
            "codigo": "001",
            "letra": "A",
            "punto_venta": "0007",
            "numero": "00007777",
            "fecha_emision": "2026-06-19",
            "moneda": "ARS",
            "cae": "86250381245054",
        },
        "emisor": {"razon_social": "DAS DACH S.A.", "cuit": "33711969859"},
        "receptor": {"razon_social": "", "cuit": "", "iva_condicion": ""},
        "importes": {"neto_gravado": 100.0, "iva_21": 21.0, "total": 121.0},
        "items": [],
        "percepciones_iibb_detalle": [],
        "ocr": {"texto": "", "confianza": None, "motor": "openrouter_ai"},
        "qr_afip": {},
        "validaciones": {"requiere_revision": False, "observaciones": []},
        "diagnostico": {},
        "extraccion_enriquecida": {
            "ai": {"enabled": True, "confidence": 0.9, "validaciones": {"ok": True, "fallas": []}},
            "fallback_usado": False,
            "fallback_tipo": None,
            "validaciones": {"ok": True, "fallas": []},
        },
    }

    class Result:
        def __init__(self):
            self.invoice = invoice
            self.trace = invoice["extraccion_enriquecida"]

    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(service, "GENERATE_XML", False)
    monkeypatch.setattr(service, "find_existing_invoice_result", lambda sha: None)
    monkeypatch.setattr(service, "extract_invoice_ai_first", lambda **kwargs: Result())
    monkeypatch.setattr(service, "extract_afip_qr", lambda *args, **kwargs: pytest.fail("QR legacy should not run before AI"))
    monkeypatch.setattr(service, "extract_text_sources", lambda *args, **kwargs: pytest.fail("OCR legacy should not run before AI"))
    monkeypatch.setattr(service, "write_invoice_staging", lambda invoice, written: {"enabled": True, "ok": True, "factura_id": 1})

    result = process_invoice_upload(
        data=b"%PDF",
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        source_metadata={},
        force=True,
    )

    assert result["status"] == "OK"
    assert result["requires_review"] is False
