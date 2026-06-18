"""Small host-side invoice parser service for the n8n Docker instance.

It avoids changing the n8n container when Docker/sudo access is unavailable.
Bind it to docker0 (172.17.0.1) so only containers on the host can call it.
"""

from __future__ import annotations

try:
    import cgi
except ModuleNotFoundError:  # pragma: no cover - Python 3.13 local tests; production uses 3.11 today.
    cgi = None
import base64
import email
import html
import imaplib
from io import BytesIO
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from invoice_parser_helpers import atomic_write_files, build_invoice_json, sha256_bytes, write_invoice_staging, build_diagnostico, write_debug_text_files, classify_document_type, NON_INVOICE_TYPES


HOST = os.environ.get("INVOICE_HELPER_HOST", "172.17.0.1")
PORT = int(os.environ.get("INVOICE_HELPER_PORT", "8765"))
OUTPUT_DIR = os.environ.get("INVOICE_OUTPUT_DIR", "/home/ferreteria/n8n/data/facturas_parseadas")
GENERATE_XML = os.environ.get("INVOICE_GENERATE_XML", "true").lower() == "true"
QR_MAX_PAGES = int(os.environ.get("INVOICE_QR_MAX_PAGES", "2"))
QR_ZBAR_TIMEOUT_SECONDS = int(os.environ.get("INVOICE_QR_ZBAR_TIMEOUT_SECONDS", "2"))
QR_MAX_VARIANTS = int(os.environ.get("INVOICE_QR_MAX_VARIANTS", "10"))
HELPER_VERSION = "2026-06-11-async-queue"
QUEUE_DIR = Path(os.environ.get("INVOICE_QUEUE_DIR", str(Path(OUTPUT_DIR) / "cola")))
JOB_QUEUE: "queue.Queue[dict[str, object]]" = queue.Queue()
IMAP_POLL_ENABLED = os.environ.get("INVOICE_EMAIL_IMAP_POLL_ENABLED", "false").lower() == "true"
IMAP_POLL_INTERVAL_SECONDS = int(os.environ.get("INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS", "60"))
ADMIN_TOKEN = os.environ.get("INVOICE_ADMIN_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceParserVFP/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path.startswith("/health"):
            self._handle_health()
            return
        if path.startswith("/admin/"):
            if not self._admin_authorized(query):
                return
            self._handle_admin_route(path, query)
            return
        if not self._admin_authorized(query):
            return
        if path == "/invoices" or path == "/invoices/review":
            self._handle_list_invoices(path, query)
            return
        if path.startswith("/invoices/by-sha/"):
            sha = path.split("/invoices/by-sha/", 1)[1]
            self._handle_invoice_by_sha(sha)
            return
        if path.startswith("/invoices/"):
            invoice_id = path.split("/invoices/", 1)[1]
            self._handle_invoice_detail(invoice_id)
            return
        if path == "/queue/status":
            self._handle_queue_status()
            return
        if path == "/queue/jobs":
            self._handle_queue_jobs(query)
            return
        if path.startswith("/queue/jobs/"):
            job_id = path.split("/queue/jobs/", 1)[1]
            self._handle_queue_job_detail(job_id)
            return
        self.send_error(404)

    def _admin_authorized(self, query: dict[str, list[str]] | None = None) -> bool:
        if not ADMIN_TOKEN:
            return True
        token = ""
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token and query:
            qt = query.get("token", [None])[0]
            if qt:
                token = qt.strip()
        if token != ADMIN_TOKEN:
            self.send_json({"error": "Unauthorized", "status": "ERROR"}, status=401)
            return False
        return True

    def _handle_health(self) -> None:
        mysql_keys = [
            "FASA_MYSQL_HOST",
            "FASA_MYSQL_PORT",
            "FASA_MYSQL_DATABASE",
            "FASA_MYSQL_USER",
            "FASA_MYSQL_PASSWORD",
        ]
        self.send_json(
            {
                "status": "OK",
                "version": HELPER_VERSION,
                "output_dir": OUTPUT_DIR,
                "queue_dir": str(QUEUE_DIR),
                "queue_size": JOB_QUEUE.qsize(),
                "imap_poll_enabled": IMAP_POLL_ENABLED,
                "qr_decoder_available": qr_decoder_available(),
                "mysql_env_configured": all(bool(os.environ.get(key)) for key in mysql_keys),
                "mysql_env_keys_present": {key: bool(os.environ.get(key)) for key in mysql_keys},
            }
        )

    def _handle_admin_route(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/admin/invoices":
            self._handle_admin_list_invoices(query)
            return
        if path.startswith("/admin/invoices/"):
            invoice_id = path.split("/admin/invoices/", 1)[1]
            self._handle_admin_invoice_detail(invoice_id)
            return
        self.send_error(404)

    def _handle_list_invoices(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            limit = max(1, min(limit, 200))
            offset = max(0, offset)
        except (ValueError, IndexError):
            limit, offset = 50, 0

        status_filter = query.get("status", [None])[0]
        requires_review = query.get("requires_review", [None])[0]
        provider_cuit = query.get("provider_cuit", [None])[0]
        provider_name = query.get("provider_name", [None])[0]
        date_from = query.get("date_from", [None])[0]
        date_to = query.get("date_to", [None])[0]
        document_type = query.get("document_type", [None])[0]
        invoice_type = query.get("invoice_type", [None])[0]
        q = query.get("q", [None])[0]

        if path == "/invoices/review":
            requires_review = "true"

        invoices = _scan_invoice_files(
            OUTPUT_DIR,
            limit=limit,
            offset=offset,
            status_filter=status_filter,
            requires_review=requires_review,
            provider_cuit=provider_cuit,
            provider_name=provider_name,
            date_from=date_from,
            date_to=date_to,
            document_type=document_type,
            invoice_type=invoice_type,
            q=q,
        )

        summary = _compute_invoices_summary(invoices["all_matched"])
        self.send_json({
            "status": "OK",
            "items": invoices["items"],
            "total": invoices["total"],
            "limit": limit,
            "offset": offset,
            "summary": summary,
        })

    def _handle_invoice_detail(self, invoice_id: str) -> None:
        if not invoice_id.isdigit():
            self.send_json({"error": "ID invalido, debe ser numerico", "status": "ERROR"}, status=400)
            return
        invoice = _load_invoice_by_id(OUTPUT_DIR, invoice_id)
        if invoice is None:
            self.send_json({"error": "Factura no encontrada", "status": "ERROR"}, status=404)
            return
        detail = _summarize_detail(invoice)
        self.send_json(detail)

    def _handle_invoice_by_sha(self, sha: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{6,64}", sha):
            self.send_json({"error": "SHA256 invalido", "status": "ERROR"}, status=400)
            return
        invoice = _load_invoice_by_sha(OUTPUT_DIR, sha)
        if invoice is None:
            self.send_json({"error": "Factura no encontrada", "status": "ERROR"}, status=404)
            return
        detail = _summarize_detail(invoice)
        self.send_json(detail)

    def _handle_queue_status(self) -> None:
        queue_dir = Path(str(QUEUE_DIR))
        pending_dir = queue_dir / "pendientes"
        processed_dir = queue_dir / "procesados"
        error_dir = queue_dir / "errores"
        pending = len(list(pending_dir.glob("*.json"))) if pending_dir.is_dir() else 0
        processing = JOB_QUEUE.qsize()
        processed = len(list(processed_dir.glob("*.json"))) if processed_dir.is_dir() else 0
        errors = len(list(error_dir.glob("*.json"))) if error_dir.is_dir() else 0
        mysql_keys = [
            "FASA_MYSQL_HOST", "FASA_MYSQL_PORT", "FASA_MYSQL_DATABASE",
            "FASA_MYSQL_USER", "FASA_MYSQL_PASSWORD",
        ]
        def _newest(d: Path) -> str:
            files = sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []
            return str(files[0].name) if files else ""
        self.send_json({
            "queue_dir": str(QUEUE_DIR),
            "pending_count": pending,
            "processed_count": processed,
            "error_count": errors,
            "newest_pending": _newest(pending_dir),
            "newest_processed": _newest(processed_dir),
            "newest_error": _newest(error_dir),
            "output_dir": OUTPUT_DIR,
            "imap_poll_enabled": IMAP_POLL_ENABLED,
            "mysql_env_configured": all(bool(os.environ.get(key)) for key in mysql_keys),
            "service_queue_size": processing,
        })

    def _handle_queue_jobs(self, query: dict[str, list[str]]) -> None:
        try:
            limit = int(query.get("limit", ["100"])[0])
            offset = int(query.get("offset", ["0"])[0])
            limit = max(1, min(limit, 200))
            offset = max(0, offset)
        except (ValueError, IndexError):
            limit, offset = 100, 0
        status_filter = query.get("status", [None])[0]
        jobs: list[dict[str, object]] = []
        for subdir_name in ("pendientes", "procesados", "errores"):
            if status_filter and status_filter.lower() != subdir_name:
                continue
            subdir = QUEUE_DIR / subdir_name
            if not subdir.is_dir():
                continue
            for meta_file in sorted(subdir.glob("*.json"), reverse=True)[:limit + offset]:
                try:
                    jd = json.loads(meta_file.read_text(encoding="utf-8"))
                    jd["_queue_dir"] = subdir_name
                    jobs.append(jd)
                except Exception:
                    jobs.append({"job_file": str(meta_file), "_queue_dir": subdir_name, "status": "UNREADABLE"})
        total = len(jobs)
        items = jobs[offset:offset + limit]
        self.send_json({"items": items, "total": total, "limit": limit, "offset": offset})

    def _handle_queue_job_detail(self, job_id: str) -> None:
        if "/" in job_id or "\\" in job_id or ".." in job_id:
            self.send_json({"error": "job_id invalido", "status": "ERROR"}, status=400)
            return
        for subdir_name in ("pendientes", "procesados", "errores"):
            subdir = QUEUE_DIR / subdir_name
            if not subdir.is_dir():
                continue
            for meta_file in subdir.glob("*.json"):
                if job_id in meta_file.stem:
                    try:
                        data = json.loads(meta_file.read_text(encoding="utf-8"))
                        data["_queue_dir"] = subdir_name
                        data["_job_file"] = str(meta_file)
                        sensitive = ("password", "passwd", "secret", "token", "api_key", "apikey")
                        for key in list(data.keys()):
                            if any(s in key.lower() for s in sensitive):
                                data[key] = "**REDACTED**"
                        self.send_json(data)
                        return
                    except Exception:
                        self.send_json({"job_file": str(meta_file), "_queue_dir": subdir_name, "status": "UNREADABLE"})
                        return
        self.send_json({"error": "Job no encontrado", "status": "ERROR"}, status=404)

    def _handle_admin_list_invoices(self, query: dict[str, list[str]]) -> None:
        try:
            limit = int(query.get("limit", ["100"])[0])
            limit = max(1, min(limit, 200))
        except (ValueError, IndexError):
            limit = 100
        requires_review = query.get("requires_review", [None])[0]
        status_filter = query.get("status", [None])[0]
        provider_name = query.get("provider_name", [None])[0]
        date_from = query.get("date_from", [None])[0]
        date_to = query.get("date_to", [None])[0]
        q = query.get("q", [None])[0]
        result = _scan_invoice_files(
            OUTPUT_DIR, limit=limit, offset=0,
            requires_review=requires_review, status_filter=status_filter,
            provider_name=provider_name, date_from=date_from, date_to=date_to, q=q,
        )
        summary = _compute_invoices_summary(result["all_matched"])
        rows_html = ""
        for inv in result["items"]:
            estado = str(inv.get("estado", ""))
            estado_cls = {"OK": "ok", "REVIEW_REQUIRED": "review", "ERROR": "error"}.get(estado, "")
            rr = "Si" if inv.get("requiere_revision") else ""
            total_fmt = _fmt_amount(inv.get("total"))
            fallas = "; ".join(inv.get("fallas_principales", []) or [])
            fallas_short = html.escape(fallas[:80]) + ("..." if len(fallas) > 80 else "") if fallas else ""
            proveedor = html.escape(str(inv.get("emisor_razon_social", "") or ""))
            cuit = html.escape(str(inv.get("emisor_cuit", "") or ""))
            tipo = html.escape(str(inv.get("tipo_comprobante", "") or ""))
            numero = html.escape(str(inv.get("numero_comprobante", "") or ""))
            fec = str(inv.get("fecha_emision", "") or "")
            clasif = html.escape(str(inv.get("clasificacion_documento", "") or ""))
            rec = html.escape(str(inv.get("diagnostico_recomendacion", "") or ""))
            fec_proc = str(inv.get("fecha_proceso", "") or "")[:19]
            inv_id = str(inv.get("id", ""))
            detail_link = f'<a href="/admin/invoices/{html.escape(inv_id)}">ver</a>' if inv_id else ""
            rows_html += (
                f"<tr class=\"{estado_cls}\">"
                f"<td>{detail_link}</td>"
                f"<td>{html.escape(fec_proc)}</td>"
                f"<td>{html.escape(estado)}</td>"
                f"<td>{html.escape(rr)}</td>"
                f"<td>{clasif}</td>"
                f"<td>{proveedor}</td>"
                f"<td>{cuit}</td>"
                f"<td>{tipo}</td>"
                f"<td>{numero}</td>"
                f"<td>{html.escape(fec)}</td>"
                f"<td class=\"num\">{html.escape(total_fmt)}</td>"
                f"<td>{rec}</td>"
                f"<td>{fallas_short}</td>"
                f"</tr>\n"
            )
        if not rows_html:
            rows_html = "<tr><td colspan=\"13\">No se encontraron facturas.</td></tr>"
        def _summary_row(label: str, value: object) -> str:
            return f"<span class=\"tag\">{html.escape(str(label))}: <strong>{html.escape(str(value))}</strong></span>"
        summary_html = (
            _summary_row("Total", summary["total"])
            + _summary_row("OK", summary["ok"])
            + _summary_row("Revision", summary["review_required"])
            + _summary_row("Errores", summary["errors"])
        )
        html_out = _ADMIN_LIST_HTML_TEMPLATE.format(
            summary=summary_html,
            rows=rows_html,
        )
        self._serve_html(html_out)

    def _handle_admin_invoice_detail(self, invoice_id: str) -> None:
        invoice = _load_invoice_by_id(OUTPUT_DIR, invoice_id)
        if invoice is None:
            self._serve_html(f"<html><body><h1>Factura no encontrada</h1><a href=\"/admin/invoices\">Volver</a></body></html>", status=404)
            return
        detail = _summarize_detail(invoice)
        e = html.escape
        o = detail.get("origen", {})
        c = detail.get("comprobante", {})
        em = detail.get("emisor", {})
        imp = detail.get("importes", {})
        v = detail.get("validaciones", {})
        dg = detail.get("diagnostico", {})
        ee = detail.get("extraccion_enriquecida", {})
        fs = detail.get("files", {})
        has_debug = "Si" if fs.get("has_debug") else "No"
        html_out = _ADMIN_DETAIL_HTML_TEMPLATE.format(
            volver='<a href="/admin/invoices">&larr; Volver al listado</a>',
            id=e(str(invoice_id)),
            estado=e(str(detail.get("estado", ""))),
            requiere_revision="Si" if detail.get("requiere_revision") else "No",
            clasificacion=e(str(o.get("clasificacion_documento", {}).get("tipo_documento", ""))),
            proveedor=e(str(em.get("razon_social", "") or "")),
            cuit=e(str(em.get("cuit", "") or "")),
            tipo_comprobante=e(str(c.get("tipo", "") or "")),
            letra=e(str(c.get("letra", "") or "")),
            punto_venta=e(str(c.get("punto_venta", "") or "")),
            numero=e(str(c.get("numero", "") or "")),
            fecha_emision=e(str(c.get("fecha_emision", "") or "")),
            vto=e(str(c.get("fecha_vencimiento", "") or "")),
            moneda=e(str(c.get("moneda", "ARS") or "")),
            total=e(_fmt_amount(imp.get("total", 0))),
            neto_gravado=e(_fmt_amount(imp.get("neto_gravado", 0))),
            iva_21=e(_fmt_amount(imp.get("iva_21", 0))),
            iva_105=e(_fmt_amount(imp.get("iva_105", 0))),
            percepciones=e(_fmt_amount(imp.get("percepciones_iibb", 0))),
            cae=e(str(c.get("cae", "") or "")),
            cae_vto=e(str(c.get("cae_vencimiento", "") or "")),
            qr_detectado="Si" if detail.get("qr_afip") else "No",
            recomendacion=e(str(dg.get("recomendacion", "") or "")),
            fallas=e("; ".join(v.get("observaciones", []) or [])),
            requiere_confirmacion="Si" if v.get("requiere_revision") else "No",
            perfil=e(str(ee.get("perfil_aplicado", "") or "")),
            email_from=e(str(o.get("email", {}).get("from", "") or "")),
            email_subject=e(str(o.get("email", {}).get("subject", "") or "")),
            email_date=e(str(o.get("email", {}).get("date", "") or "")),
            source_type=e(str(o.get("tipo", "") or "")),
            archivo_original=e(str(o.get("archivo_original", "") or "")),
            json_file=e(str(fs.get("json_file", "") or "")),
            xml_file=e(str(fs.get("xml_file", "") or "")),
            original_file=e(str(fs.get("original_file", "") or "")),
            has_original="Si" if fs.get("has_original") else "No",
            has_debug=has_debug,
            ocr_text_omitted="Si" if detail.get("ocr_text_omitted") else "No",
            ocr_chars=e(str(detail.get("ocr_chars", "") or "")),
        )
        self._serve_html(html_out)

    def _serve_html(self, html_content: str, status: int = 200) -> None:
        body = html_content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_POST(self) -> None:
        if not (self.path.startswith("/parse") or self.path.startswith("/enqueue")):
            self.send_error(404)
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            source_type = query.get("source_type", ["webhook"])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            upload = self.read_upload(query)
            if self.path.startswith("/enqueue"):
                job = enqueue_invoice_job(
                    data=upload["data"],
                    source_type=source_type,
                    original_filename=upload["original_filename"],
                    mime_type=upload["mime_type"],
                    source_metadata=upload["source_metadata"],
                    force=force,
                )
                self.send_json(job)
                return

            result = process_invoice_upload(
                data=upload["data"],
                source_type=source_type,
                original_filename=upload["original_filename"],
                mime_type=upload["mime_type"],
                source_metadata=upload["source_metadata"],
                force=force,
            )
            self.send_json(result)
        except Exception as exc:
            self.send_json({"status": "ERROR", "error": str(exc)}, status=500)

    def read_upload(self, query: dict[str, list[str]]) -> dict[str, object]:
        content_type = self.headers.get("content-type", "")
        if content_type.lower().startswith("multipart/"):
            fields = self.read_fields()
            upload = fields.get("file")
            if upload is None or not getattr(upload, "file", None):
                raise ValueError("multipart field 'file' is required")
            original = Path(getattr(upload, "filename", "") or "factura.bin").name
            mime_type = getattr(upload, "type", None) or "application/octet-stream"
            data = upload.file.read()
            source_metadata = source_metadata_from_fields(fields)
        else:
            length = int(self.headers.get("content-length", "0"))
            data = self.rfile.read(length)
            original = Path(query.get("filename", ["factura.pdf"])[0] or "factura.pdf").name
            mime_type = query.get("mime_type", [content_type or "application/octet-stream"])[0]
            source_metadata = {}
        if not data:
            raise ValueError("uploaded file is empty")
        return {
            "data": data,
            "original_filename": original,
            "mime_type": mime_type,
            "source_metadata": source_metadata,
        }

    def read_fields(self) -> dict[str, cgi.FieldStorage]:
        if cgi is None:
            raise RuntimeError("multipart requiere cgi o Python <= 3.12 en este servicio")
        env = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": self.headers.get("content-type", ""),
            "CONTENT_LENGTH": self.headers.get("content-length", "0"),
        }
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env, keep_blank_values=True)
        return {key: form[key] for key in form.keys()}

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def source_metadata_from_fields(fields: dict[str, cgi.FieldStorage]) -> dict[str, object]:
    email = {
        "from": _form_value(fields, "email_from"),
        "to": _form_value(fields, "email_to"),
        "subject": _form_value(fields, "email_subject"),
        "date": _form_value(fields, "email_date"),
        "message_id": _form_value(fields, "email_message_id"),
        "attachment_name": _form_value(fields, "email_attachment_name"),
    }
    email = {key: value for key, value in email.items() if value}
    return {"email": email} if email else {}


def _form_value(fields: dict[str, cgi.FieldStorage], name: str) -> str:
    value = fields.get(name)
    if value is None:
        return ""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or getattr(value, "filename", None):
        return ""
    raw = getattr(value, "value", "")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return str(raw).strip()


def extension_from_mime_type(mime_type: str) -> str:
    value = (mime_type or "").split(";", 1)[0].strip().lower()
    return {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
    }.get(value, "bin")


def enqueue_invoice_job(
    *,
    data: bytes,
    source_type: str,
    original_filename: str,
    mime_type: str,
    source_metadata: dict[str, object],
    force: bool = False,
) -> dict[str, object]:
    sha = sha256_bytes(data)
    ext = Path(original_filename).suffix.lower().lstrip(".") or extension_from_mime_type(mime_type)
    job_id = f"{int(time.time())}-{sha[:16]}"
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    pending_dir = QUEUE_DIR / "pendientes"
    pending_dir.mkdir(parents=True, exist_ok=True)
    original_path = pending_dir / f"{job_id}.{ext}"
    metadata_path = pending_dir / f"{job_id}.json"
    original_path.write_bytes(data)
    job = {
        "job_id": job_id,
        "status": "QUEUED",
        "sha256": sha,
        "source_type": source_type,
        "original_filename": original_filename,
        "mime_type": mime_type,
        "extension": ext,
        "source_metadata": source_metadata,
        "force": force,
        "original_path": str(original_path),
        "metadata_path": str(metadata_path),
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    metadata_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    JOB_QUEUE.put(job)
    return {
        "status": "QUEUED",
        "job_id": job_id,
        "sha256": sha,
        "queue_size": JOB_QUEUE.qsize(),
        "original_filename": original_filename,
    }


def invoice_worker() -> None:
    while True:
        job = JOB_QUEUE.get()
        try:
            _process_invoice_job(job)
        except Exception:
            print("ERROR processing invoice job", traceback.format_exc(), flush=True)
        finally:
            JOB_QUEUE.task_done()


def _process_invoice_job(job: dict[str, object]) -> None:
    metadata_path = Path(str(job["metadata_path"]))
    original_path = Path(str(job["original_path"]))
    started = dict(job)
    started["status"] = "PROCESSING"
    started["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    metadata_path.write_text(json.dumps(started, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = process_invoice_upload(
            data=original_path.read_bytes(),
            source_type=str(job["source_type"]),
            original_filename=str(job["original_filename"]),
            mime_type=str(job["mime_type"]),
            source_metadata=job.get("source_metadata") if isinstance(job.get("source_metadata"), dict) else {},
            force=bool(job.get("force")),
        )
        done = dict(started)
        done.update(
            {
                "status": "DONE",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "result": result,
            }
        )
        _write_job_status(metadata_path, done, "procesados")
    except Exception as exc:
        failed = dict(started)
        failed.update(
            {
                "status": "ERROR",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        _write_job_status(metadata_path, failed, "errores")


def _write_job_status(metadata_path: Path, payload: dict[str, object], final_dir_name: str) -> None:
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    final_dir = QUEUE_DIR / final_dir_name
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / metadata_path.name
    try:
        metadata_path.replace(final_path)
    except OSError:
        final_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def imap_poll_worker() -> None:
    while True:
        try:
            poll_imap_once()
        except Exception:
            print("ERROR polling IMAP", traceback.format_exc(), flush=True)
        time.sleep(max(10, IMAP_POLL_INTERVAL_SECONDS))


def poll_imap_once() -> None:
    host = os.environ.get("INVOICE_EMAIL_IMAP_HOST") or os.environ.get("IMAP_HOST")
    user = os.environ.get("INVOICE_EMAIL_IMAP_USER") or os.environ.get("IMAP_USER")
    password = os.environ.get("INVOICE_EMAIL_IMAP_PASSWORD") or os.environ.get("IMAP_PASSWORD")
    if not all([host, user, password]):
        return
    port = int(os.environ.get("INVOICE_EMAIL_IMAP_PORT") or os.environ.get("IMAP_PORT") or 993)
    folder = os.environ.get("INVOICE_EMAIL_FOLDER", "INBOX")
    allowed_senders = _csv_values(os.environ.get("INVOICE_EMAIL_ALLOWED_SENDERS", ""))
    allowed_extensions = set(_csv_values(os.environ.get("INVOICE_EMAIL_ALLOWED_EXTENSIONS", "pdf,jpg,jpeg,png")))

    mail = imaplib.IMAP4_SSL(host, port)
    try:
        mail.login(user, password)
        mail.select(folder, readonly=False)
        typ, data = mail.search(None, "UNSEEN")
        if typ != "OK":
            return
        for msgid in (data[0] or b"").split():
            _process_imap_message(
                mail=mail,
                msgid=msgid,
                allowed_senders=allowed_senders,
                allowed_extensions=allowed_extensions,
            )
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _process_imap_message(
    *,
    mail: imaplib.IMAP4_SSL,
    msgid: bytes,
    allowed_senders: list[str],
    allowed_extensions: set[str],
) -> None:
    try:
        typ, msgdata = mail.fetch(msgid, "(UID BODY.PEEK[])")
        if typ != "OK" or not msgdata or not msgdata[0]:
            return
        uid = _extract_uid_from_fetch(msgdata[0])
        raw_bytes = msgdata[0][1]
        message = email.message_from_bytes(raw_bytes)
        sender = _decode_mime_header(message.get("from", ""))
        if not _sender_allowed(sender, allowed_senders):
            return
        email_to = _decode_mime_header(message.get("to", ""))
        email_subject = _decode_mime_header(message.get("subject", ""))
        email_date = _decode_mime_header(message.get("date", ""))
        message_id = _decode_mime_header(message.get("message-id", ""))

        has_error = False
        all_handled = True

        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = _decode_mime_header(part.get_filename() or "")
            mime_type = part.get_content_type() or "application/octet-stream"
            ext = Path(filename).suffix.lower().lstrip(".") or extension_from_mime_type(mime_type)
            if allowed_extensions and ext.lower() not in allowed_extensions:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if not filename:
                filename = f"attachment.{ext}"

            result = _enqueue_attachment_or_skip(
                payload=payload,
                filename=Path(filename).name,
                mime_type=mime_type,
                sender=sender,
                email_to=email_to,
                email_subject=email_subject,
                email_date=email_date,
                message_id=message_id,
                uid=uid,
                force=False,
            )
            if result == "error":
                has_error = True
            elif result == "skip":
                pass
        if not has_error and all_handled:
            mail.store(msgid, "+FLAGS", r"(\Seen)")
    except Exception as exc:
        print(f"error procesando mensaje IMAP {msgid!r}: {exc}", flush=True)


def _extract_uid_from_fetch(fetch_data: tuple[object, object]) -> str:
    raw = str(fetch_data[0] if fetch_data else "")
    match = re.search(r"UID\s+(\d+)", raw, re.I)
    return match.group(1) if match else ""


def _enqueue_attachment_or_skip(
    *,
    payload: bytes,
    filename: str,
    mime_type: str,
    sender: str,
    email_to: str,
    email_subject: str,
    email_date: str,
    message_id: str,
    uid: str,
    force: bool = False,
) -> str:
    sha = sha256_bytes(payload)
    source_metadata = {
        "email": {
            "from": sender,
            "to": email_to,
            "subject": email_subject,
            "date": email_date,
            "message_id": message_id,
            "imap_uid": uid,
            "attachment_name": filename,
        }
    }
    source_metadata["email"] = {k: v for k, v in source_metadata["email"].items() if v}

    if not force:
        existing = _find_sha_in_queue_or_staging(sha)
        if existing:
            print(f"sha256 duplicado (omitido): {sha[:16]}... archivo={filename}", flush=True)
            return "skip"

    try:
        enqueue_invoice_job(
            data=payload,
            source_type="email",
            original_filename=filename,
            mime_type=mime_type,
            source_metadata=source_metadata,
            force=force,
        )
        return "enqueued"
    except Exception as exc:
        print(f"error al encolar {filename}: {exc}", flush=True)
        return "error"


def _sha_in_queue_dir(sha256: str, subdir_name: str) -> bool:
    subdir = QUEUE_DIR / subdir_name
    if not subdir.is_dir():
        return False
    for meta_file in subdir.glob("*.json"):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if data.get("sha256") == sha256:
                return True
        except Exception:
            continue
    return False


def _find_sha_in_queue_or_staging(sha256: str) -> bool:
    for subdir in ("pendientes", "procesados", "errores"):
        if _sha_in_queue_dir(sha256, subdir):
            return True
    existing = find_existing_invoice_result(sha256)
    if existing:
        return True
    return False


def _csv_values(value: str) -> list[str]:
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


def _decode_mime_header(value: str) -> str:
    parts: list[str] = []
    for text, encoding in decode_header(value or ""):
        if isinstance(text, bytes):
            parts.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _sender_allowed(value: str, rules: list[str]) -> bool:
    if not rules:
        return True
    raw = (value or "").lower()
    match = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", raw)
    address = match.group(0) if match else raw.strip()
    for rule in rules:
        if rule.startswith("@") and address.endswith(rule):
            return True
        if "@" in rule and address == rule:
            return True
        if "@" not in rule and address.endswith("@" + rule):
            return True
    return False


def process_invoice_upload(
    *,
    data: bytes,
    source_type: str,
    original_filename: str,
    mime_type: str,
    source_metadata: dict[str, object],
    force: bool = False,
) -> dict[str, object]:
    ext = Path(original_filename).suffix.lower().lstrip(".") or extension_from_mime_type(mime_type)
    print(f"archivo recibido: nombre={original_filename} source_type={source_type} bytes={len(data)} force={force}", flush=True)
    sha = sha256_bytes(data)
    print(f"hash calculado: sha256={sha}", flush=True)
    if not force:
        existing = find_existing_invoice_result(sha)
        if existing:
            print(f"hash duplicado: sha256={sha} factura_id={(existing.get('staging') or {}).get('factura_id')}", flush=True)
            return dict(existing, duplicate=True)
    qr_afip = extract_afip_qr(data, ext)
    print(f"QR {'detectado' if qr_afip else 'no detectado'}", flush=True)
    text_sources = extract_text_sources(data, ext)
    ocr_text = text_sources["combined_text"]
    ocr_engine = text_sources["engine"]
    print(
        f"textos extraidos: pdf_text_chars={len(text_sources['pdf_text'])} ocr_text_chars={len(text_sources['ocr_text'])} fuente={ocr_engine}",
        flush=True,
    )
    invoice = build_invoice_json(
        ocr_text=ocr_text,
        pdf_text=text_sources["pdf_text"],
        source_type=source_type,
        original_filename=original_filename,
        mime_type=mime_type,
        sha256=sha,
        phash="",
        ocr_confidence=None if ocr_engine in {"pdftotext", "pdf_ocr", "image_ocr"} else 0,
        ocr_engine=ocr_engine,
        qr_afip=qr_afip,
        source_metadata=source_metadata,
    )

    clasificacion = classify_document_type(
        combined_text=ocr_text,
        pdf_text=text_sources["pdf_text"],
        ocr_text=text_sources["ocr_text"],
        qr_afip=qr_afip,
        filename=original_filename,
        mime_type=mime_type,
    )
    invoice.setdefault("diagnostico", {})["clasificacion_documento"] = clasificacion
    invoice.setdefault("origen", {})["clasificacion_documento"] = clasificacion

    tipo_doc = clasificacion["tipo_documento"]
    print(f"clasificacion documento: {tipo_doc} confianza={clasificacion['confianza']}", flush=True)

    if tipo_doc in NON_INVOICE_TYPES:
        invoice["validaciones"]["requiere_revision"] = True
        invoice["validaciones"]["observaciones"].append(
            f"Documento clasificado como {tipo_doc}; no se procesa como factura fiscal"
        )
        invoice["diagnostico"]["recomendacion"] = "ignorar_no_fiscal" if tipo_doc != "ILEGIBLE" else "reintentar"

    enriched = invoice.get("extraccion_enriquecida") or {}
    fields = enriched.get("campos") or {}
    found = [name for name, field in fields.items() if isinstance(field, dict) and field.get("fuente") != "vacio"]
    failures = ((enriched.get("validaciones") or {}).get("fallas") or [])
    print(f"campos encontrados: {', '.join(found)}", flush=True)
    if failures:
        print(f"validaciones fallidas: {json.dumps(failures, ensure_ascii=False)}", flush=True)

    written = atomic_write_files(
        output_dir=OUTPUT_DIR,
        invoice=invoice,
        original_bytes=data,
        original_extension=ext,
        generate_xml=GENERATE_XML,
    )
    write_debug_text_files(
        invoice=invoice,
        output_dir=OUTPUT_DIR,
        pdf_text=text_sources["pdf_text"],
        ocr_text=text_sources["ocr_text"],
        combined_text=ocr_text,
    )
    if tipo_doc in NON_INVOICE_TYPES:
        print(f"documento no fiscal ({tipo_doc}) — se omite staging para VFP", flush=True)
        staging = {
            "enabled": True, "ok": False, "factura_id": None,
            "detalle_rows": 0, "percepcion_rows": 0, "error": "",
            "omitido_por_tipo": tipo_doc,
        }
    else:
        staging = write_invoice_staging(invoice, written)
    print(f"estado final: {invoice['estado']} staging_ok={staging.get('ok')} error={staging.get('error')}", flush=True)
    diagnostico = invoice.get("diagnostico") or build_diagnostico(invoice)
    return {
        "status": invoice["estado"],
        "json_file": written["json_file"],
        "xml_file": written["xml_file"],
        "ready_file": written["ready_file"],
        "sha256": sha,
        "requires_review": invoice["validaciones"]["requiere_revision"],
        "staging": staging,
        "diagnostico": diagnostico,
    }


def find_existing_invoice_result(sha256: str) -> dict[str, object] | None:
    conn_info = _mysql_connection_info_for_service()
    if not conn_info:
        return None
    try:
        import pymysql  # type: ignore
    except Exception:
        return None
    try:
        conn = pymysql.connect(
            host=conn_info["host"],
            port=int(conn_info.get("port") or 3306),
            user=conn_info["user"],
            password=conn_info["password"],
            database=conn_info["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )
        with conn:
            with conn.cursor() as cur:
                has_file_hash = _service_table_has_column(cur, "facturas_ocr_cabecera", "file_hash")
                where_clause = "sha256 = %s OR file_hash = %s" if has_file_hash else "sha256 = %s"
                params = (sha256, sha256) if has_file_hash else (sha256,)
                cur.execute(
                    f"""
                    SELECT id, sha256, json_file, xml_file, ready_file, estado, requiere_revision
                    FROM facturas_ocr_cabecera
                    WHERE {where_clause}
                    LIMIT 1
                    """,
                    params,
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "status": row.get("estado") or "OK",
            "json_file": row.get("json_file") or "",
            "xml_file": row.get("xml_file") or None,
            "ready_file": row.get("ready_file") or "",
            "sha256": row.get("sha256") or sha256,
            "requires_review": bool(row.get("requiere_revision")),
            "staging": {"enabled": True, "ok": True, "factura_id": row.get("id"), "duplicate": True},
        }
    except Exception as exc:
        print(f"no se pudo consultar duplicado por hash: {exc}", flush=True)
        return None


def _service_table_has_column(cur: object, table_name: str, column_name: str) -> bool:
    try:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table_name, column_name),
        )
        row = cur.fetchone() or {}
        return int(row.get("n") or 0) > 0
    except Exception:
        return False


def _mysql_connection_info_for_service() -> dict[str, str] | None:
    url = os.environ.get("FASA_MYSQL_URL") or os.environ.get("MYSQL_URL") or os.environ.get("DATABASE_URL")
    if url:
        from urllib.parse import unquote

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


def extract_text_sources(data: bytes, ext: str) -> dict[str, str]:
    pdf_text = ""
    ocr_text = ""
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / f"invoice.{ext or 'bin'}"
        source_path.write_bytes(data)
        if ext != "pdf":
            from factura_ocr.extract import extract_image_text

            ocr_text = extract_image_text(source_path)
            return {"pdf_text": "", "ocr_text": ocr_text, "combined_text": ocr_text, "engine": "image_ocr"}

        from factura_ocr.extract import extract_pdf_ocr, extract_pdf_text

        try:
            pdf_text = extract_pdf_text(source_path)
        except Exception as exc:
            print(f"lectura PDF texto fallo: {exc}", flush=True)
            pdf_text = ""
        try:
            ocr_text = extract_pdf_ocr(source_path)
        except Exception as exc:
            print(f"OCR Tesseract fallo: {exc}", flush=True)
            ocr_text = ""

    if pdf_text.strip() and ocr_text.strip() and ocr_text.strip() != pdf_text.strip():
        combined = pdf_text.rstrip() + "\n\n--- OCR VISUAL ---\n" + ocr_text
        engine = "pdf_text_plus_ocr"
    elif pdf_text.strip():
        combined = pdf_text
        engine = "pdf_text"
    else:
        combined = ocr_text
        engine = "pdf_ocr"
    return {"pdf_text": pdf_text, "ocr_text": ocr_text, "combined_text": combined, "engine": engine}


def extract_text(data: bytes, ext: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / f"invoice.{ext or 'bin'}"
        source_path.write_bytes(data)
        if ext != "pdf":
            from factura_ocr.extract import extract_image_text

            return extract_image_text(source_path), "image_ocr"

        pdf_path = source_path
        from factura_ocr.extract import extract_pdf_ocr, extract_pdf_text

        text = extract_pdf_text(pdf_path)
        if text.strip():
            if not re.search(r"Raz[oó]n\s+Social|Domicilio\s+Comercial", text, flags=re.I):
                try:
                    ocr_text = extract_pdf_ocr(pdf_path)
                except Exception:
                    ocr_text = ""
                if ocr_text.strip():
                    text = text.rstrip() + "\n\n--- OCR VISUAL ---\n" + ocr_text
                    return text, "pdf_text_plus_ocr"
            return text, "pdf_text"

        return extract_pdf_ocr(pdf_path), "pdf_ocr"


def extract_afip_qr(data: bytes, ext: str) -> dict[str, object]:
    decoded_values = _decode_qr_values(data, ext)
    for raw_value in decoded_values:
        payload = _parse_afip_qr_payload(raw_value)
        if payload:
            return {
                "detectado": True,
                "url": raw_value,
                "datos": payload,
            }
    return {}


def qr_decoder_available() -> bool:
    if shutil.which("zbarimg"):
        return True
    try:
        from pyzbar.pyzbar import decode  # noqa: F401
    except Exception:
        return False
    return True


def _decode_qr_values(data: bytes, ext: str) -> list[str]:
    try:
        from PIL import Image, ImageOps
    except Exception:
        return []
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
    except Exception:
        pyzbar_decode = None

    def decode_image(image: "Image.Image") -> list[str]:
        values: list[str] = []
        for candidate in _qr_image_variants(image):
            if pyzbar_decode is not None:
                try:
                    for symbol in pyzbar_decode(candidate):
                        text = symbol.data.decode("utf-8", errors="replace").strip()
                        if text and text not in values:
                            values.append(text)
                except Exception:
                    pass
            if not values:
                for text in _decode_qr_with_zbarimg(candidate):
                    if text not in values:
                        values.append(text)
        return values

    values: list[str] = []
    if ext.lower() == "pdf":
        try:
            import fitz

            document = fitz.open(stream=data, filetype="pdf")
            for page_index in range(min(len(document), QR_MAX_PAGES)):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(BytesIO(pixmap.tobytes("png")))
                for value in decode_image(image):
                    if value not in values:
                        values.append(value)
                if values:
                    break
        except Exception:
            return values
        return values

    try:
        image = Image.open(BytesIO(data))
    except Exception:
        return []
    return decode_image(image)


def _decode_qr_with_zbarimg(image: object) -> list[str]:
    if not shutil.which("zbarimg"):
        return []
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            image.save(tmp_path, format="PNG")
            result = subprocess.run(
                ["zbarimg", "--quiet", "--raw", tmp_path],
                capture_output=True,
                text=True,
                timeout=QR_ZBAR_TIMEOUT_SECONDS,
                check=False,
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        return []


def _qr_image_variants(image: object) -> list[object]:
    from PIL import ImageEnhance, ImageFilter, ImageOps

    rgb = image.convert("RGB")
    width, height = rgb.size
    gray = ImageOps.grayscale(rgb)
    variants = [
        rgb,
        gray,
        ImageOps.autocontrast(gray),
        ImageEnhance.Contrast(gray).enhance(2.5),
        gray.filter(ImageFilter.SHARPEN),
    ]

    lower = rgb.crop((0, int(height * 0.45), width, height))
    lower_gray = ImageOps.grayscale(lower)
    variants.extend([lower, lower_gray, ImageOps.autocontrast(lower_gray), ImageEnhance.Contrast(lower_gray).enhance(2.5)])

    crop_boxes = [
        (int(width * 0.20), int(height * 0.58), int(width * 0.75), int(height * 0.98)),
        (int(width * 0.25), int(height * 0.65), int(width * 0.65), int(height * 0.96)),
        (int(width * 0.30), int(height * 0.68), int(width * 0.62), int(height * 0.95)),
    ]
    for box in crop_boxes:
        crop = rgb.crop(box)
        crop_gray = ImageOps.grayscale(crop)
        variants.extend([crop, crop_gray, ImageOps.autocontrast(crop_gray), ImageEnhance.Contrast(crop_gray).enhance(3.0)])

    enlarged = rgb.resize((width * 2, height * 2))
    variants.append(enlarged)
    for candidate in list(variants):
        w, h = candidate.size
        if w < 1800 and h < 1800:
            variants.append(candidate.resize((w * 2, h * 2)))
    return variants[:QR_MAX_VARIANTS]


def _parse_afip_qr_payload(raw_value: str) -> dict[str, object]:
    parsed = urlparse(raw_value)
    query = parse_qs(parsed.query)
    payload_values = query.get("p") or query.get("P")
    encoded = payload_values[0] if payload_values else raw_value.strip()
    encoded = encoded.strip()
    if not encoded:
        return {}

    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        payload = json.loads(decoded)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}
    if not {"cuit", "ptoVta", "tipoCmp", "nroCmp", "importe"}.intersection(payload):
        return {}
    return payload


def _fmt_amount(val: object) -> str:
    try:
        n = float(val)
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return str(val)


def _invoice_list_item(path: str) -> dict[str, object] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    o = data.get("origen", {})
    c = data.get("comprobante", {})
    e = data.get("emisor", {})
    imp = data.get("importes", {})
    v = data.get("validaciones", {})
    dg = data.get("diagnostico", {})
    cl = o.get("clasificacion_documento") or dg.get("clasificacion_documento") or {}
    email = o.get("email", {})
    sha = o.get("sha256", "")
    base_dir = Path(path).parent
    originals_dir = base_dir / "originales"
    sha8 = sha[:8] if sha else ""
    has_json = Path(path).exists()
    has_xml = (base_dir / Path(path).name.replace(".json", ".xml")).exists()
    has_ready = (base_dir / Path(path).name.replace(".json", ".ready")).exists()
    has_original = (originals_dir / f"{sha}.pdf").exists() or (originals_dir / f"{sha}.jpg").exists() or (originals_dir / f"{sha}.png").exists()
    has_debug = (base_dir / f"{sha8}_diagnostico.json").exists() or (base_dir / f"{sha8}_combined_text.txt").exists()
    ee = data.get("extraccion_enriquecida", {})
    return {
        "id": "",
        "json_file": path,
        "xml_file": str(base_dir / Path(path).name.replace(".json", ".xml")),
        "ready_file": str(base_dir / Path(path).name.replace(".json", ".ready")),
        "original_file": str(originals_dir / f"{sha}.pdf") if sha else "",
        "sha256": sha,
        "fecha_proceso": data.get("fecha_proceso", ""),
        "estado": data.get("estado", ""),
        "requiere_revision": v.get("requiere_revision", False),
        "source_type": o.get("tipo", ""),
        "archivo_original": o.get("archivo_original", ""),
        "email_from": email.get("from", ""),
        "email_subject": email.get("subject", ""),
        "emisor_razon_social": e.get("razon_social", ""),
        "emisor_cuit": e.get("cuit", ""),
        "tipo_comprobante": c.get("tipo", ""),
        "letra": c.get("letra", ""),
        "punto_venta": c.get("punto_venta", ""),
        "numero_comprobante": c.get("numero", ""),
        "fecha_emision": c.get("fecha_emision", ""),
        "total": imp.get("total", 0),
        "moneda": c.get("moneda", ""),
        "cae": c.get("cae", ""),
        "clasificacion_documento": cl.get("tipo_documento", ""),
        "qr_detectado": bool(data.get("qr_afip", {})),
        "diagnostico_recomendacion": dg.get("recomendacion", ""),
        "fallas_principales": v.get("observaciones", []),
        "perfil_proveedor_aplicado": ee.get("perfil_aplicado", ""),
        "has_json": has_json,
        "has_xml": has_xml,
        "has_ready": has_ready,
        "has_original": has_original,
        "has_debug": has_debug,
    }


def _scan_invoice_files(
    output_dir: str,
    *,
    limit: int = 50,
    offset: int = 0,
    status_filter: str | None = None,
    requires_review: str | None = None,
    provider_cuit: str | None = None,
    provider_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    document_type: str | None = None,
    invoice_type: str | None = None,
    q: str | None = None,
) -> dict[str, object]:
    base = Path(output_dir)
    json_files = sorted(base.glob("FACTURA_*.json"), reverse=True)
    all_matched: list[dict[str, object]] = []
    for idx, fpath in enumerate(json_files):
        item = _invoice_list_item(str(fpath))
        if item is None:
            continue
        if not item.get("id"):
            item["id"] = str(idx + 1)
        if status_filter and item["estado"] != status_filter:
            continue
        if requires_review is not None:
            wants_review = requires_review.lower() in ("true", "1", "yes")
            if bool(item["requiere_revision"]) != wants_review:
                continue
        if provider_cuit and provider_cuit not in str(item["emisor_cuit"]):
            continue
        if provider_name and provider_name.lower() not in str(item["emisor_razon_social"]).lower():
            continue
        fe = item["fecha_emision"]
        fe_str = str(fe) if fe is not None else ""
        if date_from and fe_str < date_from:
            continue
        if date_to and fe_str > date_to:
            continue
        if document_type and document_type.lower() != str(item["clasificacion_documento"]).lower():
            continue
        if invoice_type and invoice_type.lower() != str(item["tipo_comprobante"]).lower():
            continue
        if q:
            ql = q.lower()
            haystack = "{} {} {} {} {} {} {}".format(
                item["archivo_original"], item["emisor_razon_social"],
                item["emisor_cuit"], item["email_from"],
                item["email_subject"], item["tipo_comprobante"],
                item["numero_comprobante"],
            ).lower()
            if ql not in haystack:
                continue
        all_matched.append(item)
    total = len(all_matched)
    items = all_matched[offset:offset + limit]
    return {"items": items, "total": total, "all_matched": all_matched}


def _compute_invoices_summary(invoices: list[dict[str, object]]) -> dict[str, object]:
    ok_count = 0
    review_count = 0
    errors_count = 0
    duplicates_count = 0
    non_invoice_count = 0
    by_provider: dict[str, int] = {}
    by_falla: dict[str, int] = {}
    for inv in invoices:
        estado = str(inv.get("estado", ""))
        rr = bool(inv.get("requiere_revision"))
        clasif = str(inv.get("clasificacion_documento", ""))
        if clasif in ("NO_FISCAL", "ILEGIBLE", "OTRO", "REMITO", "PRESUPUESTO"):
            non_invoice_count += 1
        if estado == "OK" and not rr:
            ok_count += 1
        if rr or estado == "REVIEW_REQUIRED":
            review_count += 1
        if estado == "ERROR":
            errors_count += 1
        if estado == "DUPLICATE":
            duplicates_count += 1
        prov = str(inv.get("emisor_razon_social", "") or "?")
        by_provider[prov] = by_provider.get(prov, 0) + 1
        for falla in (inv.get("fallas_principales") or []):
            key = str(falla)
            by_falla[key] = by_falla.get(key, 0) + 1
    top_fallas = sorted(by_falla.items(), key=lambda x: -x[1])[:10]
    return {
        "total": len(invoices),
        "ok": ok_count,
        "review_required": review_count,
        "errors": errors_count,
        "duplicates": duplicates_count,
        "non_invoice": non_invoice_count,
        "por_proveedor": dict(sorted(by_provider.items(), key=lambda x: -x[1])[:20]),
        "fallas_frecuentes": [{"falla": k, "count": v} for k, v in top_fallas],
    }


def _summarize_detail(invoice: dict[str, object]) -> dict[str, object]:
    o = invoice.get("origen", {})
    c = invoice.get("comprobante", {})
    e = invoice.get("emisor", {})
    imp = invoice.get("importes", {})
    v = invoice.get("validaciones", {})
    dg = invoice.get("diagnostico", {})
    ee = invoice.get("extraccion_enriquecida", {})
    cl = o.get("clasificacion_documento") or dg.get("clasificacion_documento") or {}
    sha = o.get("sha256", "")
    output_path = Path(str(OUTPUT_DIR))
    originals_dir = output_path / "originales"
    sha8 = sha[:8] if sha else ""
    json_path_str = ""
    for ext in (".json",):
        candidate = output_path / f"FACTURA_*_{sha8}{ext}"
        from glob import glob
        found = glob(str(candidate))
        if found:
            json_path_str = found[0]
            break
    json_path = Path(json_path_str) if json_path_str else None
    xml_path = output_path / (json_path.name.replace(".json", ".xml")) if json_path and json_path.exists() else None
    ready_path = output_path / (json_path.name.replace(".json", ".ready")) if json_path and json_path.exists() else None
    original_path = originals_dir / f"{sha}.pdf" if sha else None
    if original_path and not original_path.exists():
        for ext in (".jpg", ".jpeg", ".png"):
            test = originals_dir / f"{sha}{ext}"
            if test.exists():
                original_path = test
                break
        else:
            original_path = None
    debug_files: list[str] = []
    if sha8:
        for pat in (f"{sha8}_diagnostico.json", f"{sha8}_combined_text.txt", f"{sha8}_pdf_text.txt", f"{sha8}_ocr_text.txt", f"{sha8}_qr.json"):
            dp = output_path / pat
            if dp.exists():
                debug_files.append(str(dp))
    detail = {
        "id": invoice.get("_staging_id", ""),
        "estado": invoice.get("estado", ""),
        "fecha_proceso": invoice.get("fecha_proceso", ""),
        "requiere_revision": v.get("requiere_revision", False),
        "clasificacion_documento": cl,
        "origen": o,
        "comprobante": c,
        "emisor": e,
        "importes": imp,
        "qr_afip": invoice.get("qr_afip", {}),
        "validaciones": v,
        "diagnostico": dg,
        "extraccion_enriquecida": ee,
        "extraccion_facturas_ocr": invoice.get("extraccion_facturas_ocr", {}),
        "files": {
            "json_file": str(json_path) if json_path else "",
            "xml_file": str(xml_path) if xml_path else "",
            "ready_file": str(ready_path) if ready_path else "",
            "original_file": str(original_path) if original_path else "",
            "debug_files": debug_files,
            "has_original": original_path is not None and original_path.exists(),
            "has_debug": len(debug_files) > 0,
        },
        "ocr_text_omitted": True,
        "ocr_chars": len(str(invoice.get("ocr", {}).get("texto", ""))),
    }
    return detail


def _load_invoice_by_id(output_dir: str, invoice_id: str) -> dict[str, object] | None:
    conn_info = _mysql_connection_info_for_service()
    if conn_info:
        try:
            import pymysql
            conn = pymysql.connect(
                host=conn_info["host"], port=int(conn_info.get("port") or 3306),
                user=conn_info["user"], password=conn_info["password"],
                database=conn_info["database"], charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=3, read_timeout=5, write_timeout=5,
            )
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, sha256, json_file FROM facturas_ocr_cabecera WHERE id = %s LIMIT 1",
                        (invoice_id,),
                    )
                    row = cur.fetchone()
            if row:
                json_path = row.get("json_file") or ""
                if json_path and Path(json_path).exists():
                    try:
                        full = json.loads(Path(json_path).read_text(encoding="utf-8"))
                        full["_staging_id"] = row.get("id")
                        return full
                    except Exception:
                        pass
                return dict(row)
        except Exception:
            pass
    base = Path(output_dir)
    try:
        idx = int(invoice_id) - 1
        files = sorted(base.glob("FACTURA_*.json"), reverse=True)
        if 0 <= idx < len(files):
            return json.loads(files[idx].read_text(encoding="utf-8"))
    except (ValueError, IndexError, OSError):
        pass
    return None


def _load_invoice_by_sha(output_dir: str, sha: str) -> dict[str, object] | None:
    sha_lower = sha.lower()
    base = Path(output_dir)
    for fpath in base.glob("FACTURA_*.json"):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            stored_sha = (data.get("origen") or {}).get("sha256", "")
            if stored_sha == sha_lower or stored_sha.startswith(sha_lower):
                return data
        except Exception:
            continue
    conn_info = _mysql_connection_info_for_service()
    if conn_info:
        try:
            import pymysql
            conn = pymysql.connect(
                host=conn_info["host"], port=int(conn_info.get("port") or 3306),
                user=conn_info["user"], password=conn_info["password"],
                database=conn_info["database"], charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=3, read_timeout=5, write_timeout=5,
            )
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, sha256, json_file FROM facturas_ocr_cabecera WHERE sha256 LIKE CONCAT(%s, '%') OR file_hash LIKE CONCAT(%s, '%') LIMIT 1",
                        (sha_lower, sha_lower),
                    )
                    row = cur.fetchone()
            if row:
                json_path = row.get("json_file") or ""
                if json_path and Path(json_path).exists():
                    try:
                        full = json.loads(Path(json_path).read_text(encoding="utf-8"))
                        full["_staging_id"] = row.get("id")
                        return full
                    except Exception:
                        pass
                return dict(row)
        except Exception:
            pass
    return None


_ADMIN_LIST_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Facturas procesadas</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:20px;background:#f5f5f5;color:#222}}
h1{{font-size:1.4em;margin:0 0 12px 0}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.tag{{background:#fff;padding:6px 14px;border-radius:6px;border:1px solid #ddd;font-size:0.9em}}
.filters{{margin-bottom:12px}}
.filters form{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.filters input,.filters select{{padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:0.9em}}
.filters button{{padding:4px 12px;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{padding:6px 10px;text-align:left;border-bottom:1px solid #eee;font-size:0.85em;white-space:nowrap}}
th{{background:#f0f0f0;font-weight:600}}
tr:hover{{background:#f9f9f9}}
tr.ok td{{}}
tr.review td{{background:#fff8e1}}
tr.error td{{background:#ffebee}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.fallas{{max-width:200px;overflow:hidden;text-overflow:ellipsis}}
a{{color:#2563eb;text-decoration:none}}
a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<h1>Facturas procesadas</h1>
<div class="summary">{summary}</div>
<div class="filters">
<form method="get" action="/admin/invoices">
<input name="q" placeholder="Buscar..." size="20">
<select name="requires_review">
<option value="">Todos</option>
<option value="true">Requiere revision</option>
</select>
<select name="status">
<option value="">Todos los estados</option>
<option value="OK">OK</option>
<option value="REVIEW_REQUIRED">REVIEW_REQUIRED</option>
<option value="ERROR">ERROR</option>
</select>
<input name="provider_name" placeholder="Proveedor..." size="15">
<input name="date_from" placeholder="Desde (YYYY-MM-DD)" size="12">
<input name="date_to" placeholder="Hasta (YYYY-MM-DD)" size="12">
<button type="submit">Filtrar</button>
</form>
</div>
<table>
<thead><tr>
<th>ID</th><th>Fecha proc.</th><th>Estado</th><th>Rev.</th><th>Clasif.</th>
<th>Proveedor</th><th>CUIT</th><th>Tipo</th><th>Numero</th><th>Fecha</th>
<th>Total</th><th>Recom.</th><th>Fallas</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
<p style="margin-top:12px;font-size:0.85em;color:#666">
<a href="/admin/invoices">Actualizar</a>
</p>
<p style="font-size:0.8em;color:#999">
API: <a href="/invoices">/invoices</a> | <a href="/queue/status">/queue/status</a>
</p>
<p style="font-size:0.8em;color:#999">
TODO Issue #22: agregar endpoints seguros para visualizar/descargar originales y evidencia OCR.
</p>
</body>
</html>"""

_ADMIN_DETAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Factura #{id}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:20px;background:#f5f5f5;color:#222}}
h1{{font-size:1.3em;margin:0 0 16px 0}}
.card{{background:#fff;border-radius:6px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{font-size:1.1em;margin:0 0 10px 0;color:#444;border-bottom:1px solid #eee;padding-bottom:6px}}
table.detail{{width:100%;border-collapse:collapse}}
table.detail td{{padding:4px 10px;font-size:0.9em;vertical-align:top}}
table.detail td:first-child{{font-weight:600;width:180px;color:#555}}
.path{{font-family:monospace;font-size:0.8em;word-break:break-all;color:#666}}
a{{color:#2563eb;text-decoration:none}}
a:hover{{text-decoration:underline}}
.volver{{margin-bottom:16px}}
</style>
</head>
<body>
<div class="volver">{volver}</div>
<h1>Factura #{id}</h1>
<div class="card">
<h2>Datos principales</h2>
<table class="detail">
<tr><td>Estado</td><td>{estado}</td></tr>
<tr><td>Requiere revision</td><td>{requiere_revision}</td></tr>
<tr><td>Clasificacion</td><td>{clasificacion}</td></tr>
<tr><td>Proveedor</td><td>{proveedor}</td></tr>
<tr><td>CUIT</td><td>{cuit}</td></tr>
<tr><td>Tipo</td><td>{tipo_comprobante} {letra}</td></tr>
<tr><td>Punto venta / Numero</td><td>{punto_venta}-{numero}</td></tr>
<tr><td>Fecha emision</td><td>{fecha_emision}</td></tr>
<tr><td>Vencimiento</td><td>{vto}</td></tr>
<tr><td>Moneda</td><td>{moneda}</td></tr>
<tr><td>Total</td><td><strong>{total}</strong></td></tr>
<tr><td>CAE</td><td>{cae}</td></tr>
<tr><td>CAE vencimiento</td><td>{cae_vto}</td></tr>
<tr><td>QR AFIP detectado</td><td>{qr_detectado}</td></tr>
</table>
</div>
<div class="card">
<h2>Importes</h2>
<table class="detail">
<tr><td>Neto gravado</td><td>{neto_gravado}</td></tr>
<tr><td>IVA 21%</td><td>{iva_21}</td></tr>
<tr><td>IVA 10.5%</td><td>{iva_105}</td></tr>
<tr><td>Percepciones IIBB</td><td>{percepciones}</td></tr>
</table>
</div>
<div class="card">
<h2>Diagnostico</h2>
<table class="detail">
<tr><td>Recomendacion</td><td>{recomendacion}</td></tr>
<tr><td>Fallas</td><td>{fallas}</td></tr>
<tr><td>Requiere confirmacion</td><td>{requiere_confirmacion}</td></tr>
</table>
</div>
<div class="card">
<h2>Extraccion</h2>
<table class="detail">
<tr><td>Perfil aplicado</td><td>{perfil}</td></tr>
<tr><td>OCR omitido</td><td>{ocr_text_omitted}</td></tr>
<tr><td>OCR chars</td><td>{ocr_chars}</td></tr>
</table>
</div>
<div class="card">
<h2>Email / Origen</h2>
<table class="detail">
<tr><td>Source type</td><td>{source_type}</td></tr>
<tr><td>Archivo original</td><td>{archivo_original}</td></tr>
<tr><td>Email from</td><td>{email_from}</td></tr>
<tr><td>Email subject</td><td>{email_subject}</td></tr>
<tr><td>Email date</td><td>{email_date}</td></tr>
</table>
</div>
<div class="card">
<h2>Archivos</h2>
<table class="detail">
<tr><td>JSON</td><td class="path">{json_file}</td></tr>
<tr><td>XML</td><td class="path">{xml_file}</td></tr>
<tr><td>Original</td><td class="path">{original_file}</td></tr>
<tr><td>Has original</td><td>{has_original}</td></tr>
<tr><td>Has debug</td><td>{has_debug}</td></tr>
</table>
</div>
<p style="font-size:0.8em;color:#999">
TODO Issue #22: agregar endpoints seguros para visualizar/descargar originales y evidencia OCR.
</p>
</body>
</html>"""


def main() -> None:
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=invoice_worker, name="invoice-worker", daemon=True).start()
    if IMAP_POLL_ENABLED:
        threading.Thread(target=imap_poll_worker, name="imap-poll-worker", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Invoice parser helper listening on http://{HOST}:{PORT} output_dir={OUTPUT_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
