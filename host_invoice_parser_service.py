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

from invoice_parser_helpers import atomic_write_files, build_invoice_json, sha256_bytes, write_invoice_staging


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


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceParserVFP/1.0"

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
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
            return
        self.send_error(404)

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
            typ, msgdata = mail.fetch(msgid, "(BODY.PEEK[])")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            message = email.message_from_bytes(msgdata[0][1])
            sender = _decode_mime_header(message.get("from", ""))
            if not _sender_allowed(sender, allowed_senders):
                continue
            queued = 0
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
                    filename = f"attachment_{queued}.{ext}"
                enqueue_invoice_job(
                    data=payload,
                    source_type="email",
                    original_filename=Path(filename).name,
                    mime_type=mime_type,
                    source_metadata={
                        "email": {
                            "from": sender,
                            "to": _decode_mime_header(message.get("to", "")),
                            "subject": _decode_mime_header(message.get("subject", "")),
                            "date": _decode_mime_header(message.get("date", "")),
                            "message_id": _decode_mime_header(message.get("message-id", "")),
                            "attachment_name": Path(filename).name,
                        }
                    },
                )
                queued += 1
            if queued:
                mail.store(msgid, "+FLAGS", r"(\Seen)")
    finally:
        try:
            mail.logout()
        except Exception:
            pass


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
    enriched = invoice.get("extraccion_enriquecida") or {}
    fields = enriched.get("campos") or {}
    found = [name for name, field in fields.items() if isinstance(field, dict) and field.get("fuente") != "vacio"]
    failures = ((enriched.get("validaciones") or {}).get("fallas") or [])
    print(f"campos encontrados: {', '.join(found)}", flush=True)
    if failures:
        print(f"validaciones fallidas: {json.dumps(failures, ensure_ascii=False)}", flush=True)
    if not ocr_text.strip():
        invoice["validaciones"]["requiere_revision"] = True
        invoice["validaciones"]["observaciones"].append("No se pudo extraer texto del archivo")

    written = atomic_write_files(
        output_dir=OUTPUT_DIR,
        invoice=invoice,
        original_bytes=data,
        original_extension=ext,
        generate_xml=GENERATE_XML,
    )
    staging = write_invoice_staging(invoice, written)
    print(f"estado final: {invoice['estado']} staging_ok={staging.get('ok')} error={staging.get('error')}", flush=True)
    return {
        "status": invoice["estado"],
        "json_file": written["json_file"],
        "xml_file": written["xml_file"],
        "ready_file": written["ready_file"],
        "sha256": sha,
        "requires_review": invoice["validaciones"]["requiere_revision"],
        "staging": staging,
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
