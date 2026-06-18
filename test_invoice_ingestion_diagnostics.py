from __future__ import annotations

import base64
import json
import email
import email.encoders
import email.message
import email.mime.base
import email.mime.multipart
import sys
import types
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.modules.setdefault("cgi", types.SimpleNamespace(FieldStorage=object))
import host_invoice_parser_service as service
from invoice_parser_helpers import build_invoice_json, build_diagnostico, sha256_bytes, write_debug_text_files


SAMPLE_TEXT = """ACME SRL
CUIT: 30-50000007-0
FACTURA A
Punto de Venta: 0003 Comp. Nro: 00000042
Fecha de Emision: 12/06/2026
Importe Neto Gravado: 1000,00
IVA 21% 210,00
Percepciones IIBB Misiones 30,00
Otros impuestos: 5,00
Total: $ 1245,00
CAE: 12345678901234
Fecha Vto. CAE: 22/06/2026
"""


# ---------- metadata IMAP por adjunto ----------

def test_enqueue_attachment_or_skip_includes_full_metadata() -> None:
    payload = b"fake pdf content"
    filename = "factura_1234.pdf"
    with patch.object(service, "enqueue_invoice_job") as mock_enqueue:
        result = service._enqueue_attachment_or_skip(
            payload=payload,
            filename=filename,
            mime_type="application/pdf",
            sender="compras@fasa.ar",
            email_to="facturas@fasa.ar",
            email_subject="Factura proveedor",
            email_date="Thu, 18 Jun 2026 10:00:00 +0000",
            message_id="<abc123@mail.fasa.ar>",
            uid="42",
            force=True,
        )
    assert result == "enqueued"
    assert mock_enqueue.called
    _call_kwargs = mock_enqueue.call_args[1]
    meta = _call_kwargs["source_metadata"]["email"]
    assert meta["from"] == "compras@fasa.ar"
    assert meta["to"] == "facturas@fasa.ar"
    assert meta["subject"] == "Factura proveedor"
    assert meta["date"] == "Thu, 18 Jun 2026 10:00:00 +0000"
    assert meta["message_id"] == "<abc123@mail.fasa.ar>"
    assert meta["imap_uid"] == "42"
    assert meta["attachment_name"] == filename
    assert _call_kwargs["data"] == payload


def test_enqueue_attachment_or_skip_strips_empty_metadata() -> None:
    payload = b"data"
    with patch.object(service, "enqueue_invoice_job"):
        result = service._enqueue_attachment_or_skip(
            payload=payload,
            filename="inv.pdf",
            mime_type="application/pdf",
            sender="",
            email_to="",
            email_subject="",
            email_date="",
            message_id="",
            uid="",
            force=True,
        )
    assert result == "enqueued"


# ---------- deduplicacion por sha256 ----------

def test_find_sha_in_queue_or_staging_finds_in_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    pending = tmp_path / "pendientes"
    pending.mkdir(parents=True)
    sha = "aaabbbcccddd111222333"
    meta = {"sha256": sha, "status": "QUEUED"}
    (pending / "job1.json").write_text(json.dumps(meta), encoding="utf-8")
    assert service._find_sha_in_queue_or_staging(sha) is True


def test_find_sha_in_queue_or_staging_finds_in_procesados(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    procesados = tmp_path / "procesados"
    procesados.mkdir(parents=True)
    sha = "zzzyyyxxx999"
    meta = {"sha256": sha, "status": "DONE"}
    (procesados / "job2.json").write_text(json.dumps(meta), encoding="utf-8")
    assert service._find_sha_in_queue_or_staging(sha) is True


def test_find_sha_in_queue_or_staging_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    (tmp_path / "pendientes").mkdir(parents=True)
    (tmp_path / "procesados").mkdir(parents=True)
    (tmp_path / "errores").mkdir(parents=True)
    assert service._find_sha_in_queue_or_staging("nonexistent") is False


def test_find_sha_in_queue_or_staging_falls_back_to_mysql(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    (tmp_path / "pendientes").mkdir()
    (tmp_path / "procesados").mkdir()
    (tmp_path / "errores").mkdir()
    monkeypatch.setattr(service, "find_existing_invoice_result", lambda sha: {"factura_id": 1})
    assert service._find_sha_in_queue_or_staging("somehash") is True


def test_find_sha_in_queue_or_staging_mysql_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    (tmp_path / "pendientes").mkdir()
    (tmp_path / "procesados").mkdir()
    (tmp_path / "errores").mkdir()
    monkeypatch.setattr(service, "find_existing_invoice_result", lambda sha: None)
    assert service._find_sha_in_queue_or_staging("somehash") is False


def test_enqueue_attachment_or_skip_skips_duplicate_in_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    pending = tmp_path / "pendientes"
    pending.mkdir(parents=True)
    payload = b"same content"
    sha = sha256_bytes(payload)
    (pending / "existing.json").write_text(json.dumps({"sha256": sha}), encoding="utf-8")
    result = service._enqueue_attachment_or_skip(
        payload=payload,
        filename="dup.pdf",
        mime_type="application/pdf",
        sender="test@fasa.ar",
        email_to="",
        email_subject="",
        email_date="",
        message_id="",
        uid="",
        force=False,
    )
    assert result == "skip"


def test_enqueue_attachment_or_skip_force_overrides_dedup(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    pending = tmp_path / "pendientes"
    pending.mkdir(parents=True)
    payload = b"same content"
    sha = sha256_bytes(payload)
    (pending / "existing.json").write_text(json.dumps({"sha256": sha}), encoding="utf-8")
    with patch.object(service, "enqueue_invoice_job") as mock_enq:
        result = service._enqueue_attachment_or_skip(
            payload=payload,
            filename="forced.pdf",
            mime_type="application/pdf",
            sender="test@fasa.ar",
            email_to="",
            email_subject="",
            email_date="",
            message_id="",
            uid="",
            force=True,
        )
    assert result == "enqueued"
    assert mock_enq.called


# ---------- marca como leido solo si no hay error ----------

def test_process_imap_message_all_attachments_ok_marks_read() -> None:
    msg = email.message_from_bytes(_sample_email_bytes())
    mail = MagicMock()
    mail.fetch.return_value = ("OK", [(b"(UID 99 BODY[] {1024}", msg.as_bytes())])
    service._process_imap_message(
        mail=mail,
        msgid=b"1",
        allowed_senders=["@fasa.ar"],
        allowed_extensions={"pdf"},
    )
    mail.store.assert_called_once_with(b"1", "+FLAGS", r"(\Seen)")


def test_process_imap_message_error_does_not_mark_read() -> None:
    msg = email.message_from_bytes(_sample_email_bytes())
    mail = MagicMock()
    mail.fetch.return_value = ("OK", [(b"(UID 99 BODY[] {1024}", msg.as_bytes())])
    with patch.object(service, "_enqueue_attachment_or_skip", return_value="error"):
        service._process_imap_message(
            mail=mail,
            msgid=b"1",
            allowed_senders=["@fasa.ar"],
            allowed_extensions={"pdf"},
        )
    mail.store.assert_not_called()


def test_process_imap_message_all_duplicates_allows_read() -> None:
    msg = email.message_from_bytes(_sample_email_bytes())
    mail = MagicMock()
    mail.fetch.return_value = ("OK", [(b"(UID 99 BODY[] {1024}", msg.as_bytes())])
    with patch.object(service, "_enqueue_attachment_or_skip", return_value="skip"):
        service._process_imap_message(
            mail=mail,
            msgid=b"1",
            allowed_senders=["@fasa.ar"],
            allowed_extensions={"pdf"},
        )
    mail.store.assert_called_once_with(b"1", "+FLAGS", r"(\Seen)")


def test_process_imap_message_partial_error_does_not_mark_read() -> None:
    msg = email.message_from_bytes(_sample_email_two_attachments_bytes())
    mail = MagicMock()
    mail.fetch.return_value = ("OK", [(b"(UID 100 BODY[] {2048}", msg.as_bytes())])
    results = {"return_value": "enqueued"}
    def side_effect(**kwargs: object) -> str:
        if "error.pdf" in str(kwargs.get("filename", "")):
            return "error"
        return "enqueued"
    with patch.object(service, "_enqueue_attachment_or_skip", side_effect=side_effect):
        service._process_imap_message(
            mail=mail,
            msgid=b"2",
            allowed_senders=["@fasa.ar"],
            allowed_extensions={"pdf"},
        )
    mail.store.assert_not_called()


def test_process_imap_message_skip_and_enqueued_allows_read() -> None:
    msg = email.message_from_bytes(_sample_email_two_attachments_bytes())
    mail = MagicMock()
    mail.fetch.return_value = ("OK", [(b"(UID 101 BODY[] {2048}", msg.as_bytes())])
    results: list[str] = ["skip"]
    def side_effect(**kwargs: object) -> str:
        return results.pop(0) if results else "enqueued"
    with patch.object(service, "_enqueue_attachment_or_skip", side_effect=side_effect):
        service._process_imap_message(
            mail=mail,
            msgid=b"3",
            allowed_senders=["@fasa.ar"],
            allowed_extensions={"pdf"},
        )
    mail.store.assert_called_once_with(b"3", "+FLAGS", r"(\Seen)")


# ---------- diagnostico OK ----------

def test_diagnostico_ok() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
        qr_afip={},
    )
    diag = invoice["diagnostico"]
    assert diag["estado"] == "OK"
    assert diag["requiere_revision"] is False
    assert diag["recomendacion"] == "aceptar"
    assert diag["qr_detectado"] is False
    assert "proveedor_cuit" in diag["campos_criticos"]["encontrados"]
    assert diag["campos_criticos"]["faltantes"] == []
    assert diag["campos"]["total"] == {"valor": "1245.00", "fuente": "pdf_text", "confianza": 85, "metodo": "regex_total", "evidencia": "Total: $ 1245,00"}


# ---------- diagnostico REVIEW_REQUIRED por campo critico faltante ----------

def test_diagnostico_missing_critical_field() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT.replace("CUIT: 30-50000007-0\n", ""),
        source_type="email",
        original_filename="sin_cuit.pdf",
        mime_type="application/pdf",
        sha256="b" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT.replace("CUIT: 30-50000007-0\n", ""),
        qr_afip={},
    )
    diag = invoice["diagnostico"]
    assert diag["estado"] == "REVIEW_REQUIRED"
    assert diag["requiere_revision"] is True
    assert diag["recomendacion"] == "revisar_manualmente"
    assert "proveedor_cuit" in diag["campos_criticos"]["faltantes"]


# ---------- diagnostico con TOTAL_MISMATCH ----------

def test_diagnostico_total_mismatch() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT.replace("Total: $ 1245,00", "Total: $ 9999,00"),
        source_type="email",
        original_filename="descuadrada.pdf",
        mime_type="application/pdf",
        sha256="c" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT.replace("Total: $ 1245,00", "Total: $ 9999,00"),
        qr_afip={},
    )
    diag = invoice["diagnostico"]
    assert diag["requiere_revision"] is True
    assert diag["recomendacion"] == "revisar_manualmente"
    assert diag["balance_importes"]["ok"] is False
    codigos = [f["codigo"] for f in diag["fallas"]]
    assert "TOTAL_MISMATCH" in codigos


# ---------- diagnostico con QR_OCR_MISMATCH ----------

def test_diagnostico_qr_ocr_mismatch() -> None:
    qr_afip = {
        "detectado": True,
        "url": "https://www.afip.gob.ar/fe/qr/?p=abc",
        "datos": {
            "ver": 1,
            "fecha": "2026-06-13",
            "cuit": 30500000070,
            "ptoVta": 4,
            "tipoCmp": 1,
            "nroCmp": 99,
            "importe": 1300.50,
            "moneda": "PES",
            "codAut": 99998888777766,
        },
    }
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="d" * 64,
        ocr_engine="ocr",
        pdf_text="",
        qr_afip=qr_afip,
    )
    diag = invoice["diagnostico"]
    assert diag["requiere_revision"] is True
    assert diag["recomendacion"] == "revisar_manualmente"
    assert diag["qr_detectado"] is True
    codigos = [f["codigo"] for f in diag["fallas"]]
    assert "QR_OCR_MISMATCH" in codigos


# ---------- diagnostico duplicado ----------

def test_diagnostico_duplicado() -> None:
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="e" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
        duplicate=True,
    )
    diag = invoice["diagnostico"]
    assert diag["estado"] == "DUPLICADO"
    assert diag["recomendacion"] == "ignorar_duplicado"


# ---------- diagnostico sin texto ----------

def test_diagnostico_sin_texto() -> None:
    invoice = build_invoice_json(
        ocr_text="",
        source_type="email",
        original_filename="vacio.pdf",
        mime_type="application/pdf",
        sha256="f" * 64,
        ocr_engine="pdf_text",
        pdf_text="",
        qr_afip={},
    )
    diag = invoice["diagnostico"]
    assert diag["requiere_revision"] is True
    assert diag["recomendacion"] == "reintentar"
    assert diag["ocr_text_chars"] == 0


# ---------- helpers ----------

def test_extract_uid_from_fetch() -> None:
    uid = service._extract_uid_from_fetch((b"(UID 123 BODY[] {100}", b"data"))
    assert uid == "123"


def test_sha_in_queue_dir_checks_json_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    errors = tmp_path / "errores"
    errors.mkdir(parents=True)
    sha = "known_sha_123"
    (errors / "e1.json").write_text(json.dumps({"sha256": sha}), encoding="utf-8")
    assert service._sha_in_queue_dir(sha, "errores") is True
    assert service._sha_in_queue_dir(sha, "pendientes") is False


# ---------- sample email builders ----------

def _sample_email_bytes() -> bytes:
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = "compras@fasa.ar"
    msg["To"] = "facturas@fasa.ar"
    msg["Subject"] = "Factura"
    msg["Message-ID"] = "<abc@fasa.ar>"
    msg["Date"] = "Thu, 18 Jun 2026 10:00:00 +0000"
    attachment = email.mime.base.MIMEBase("application", "pdf")
    attachment.set_payload(b"fake invoice pdf")
    email.encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename="factura_001.pdf")
    msg.attach(attachment)
    return msg.as_bytes()


def _sample_email_two_attachments_bytes() -> bytes:
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = "compras@fasa.ar"
    msg["To"] = "facturas@fasa.ar"
    msg["Subject"] = "Facturas multiples"
    msg["Message-ID"] = "<def@fasa.ar>"
    msg["Date"] = "Thu, 18 Jun 2026 11:00:00 +0000"
    for name in ("ok.pdf", "error.pdf"):
        attachment = email.mime.base.MIMEBase("application", "pdf")
        attachment.set_payload(f"content {name}".encode())
        email.encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(attachment)
    return msg.as_bytes()


# ========== debug text evidence files ==========

def test_write_debug_text_files_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INVOICE_WRITE_DEBUG_TEXTS", raising=False)
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )
    files = write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text=SAMPLE_TEXT,
        ocr_text=SAMPLE_TEXT,
        combined_text=SAMPLE_TEXT,
    )
    assert files == {}


def test_write_debug_text_files_creates_expected_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="aabbccdd" + "0" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )
    files = write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text="pdf content",
        ocr_text="ocr content",
        combined_text="combined content",
    )
    assert "pdf_text" in files
    assert "ocr_text" in files
    assert "combined_text" in files
    assert "diagnostico" in files
    assert Path(files["pdf_text"]).exists()
    assert Path(files["ocr_text"]).exists()
    assert Path(files["combined_text"]).exists()
    assert Path(files["diagnostico"]).exists()
    assert Path(files["pdf_text"]).read_text(encoding="utf-8") == "pdf content"
    assert Path(files["ocr_text"]).read_text(encoding="utf-8") == "ocr content"
    assert Path(files["combined_text"]).read_text(encoding="utf-8") == "combined content"


def test_write_debug_text_files_writes_qr_when_detected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    qr_afip = {
        "detectado": True,
        "url": "https://www.afip.gob.ar/fe/qr/?p=abc",
        "datos": {"ver": 1, "cuit": 30500000070, "importe": 1245.00},
    }
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="bbccddee" + "1" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
        qr_afip=qr_afip,
    )
    files = write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text="",
        ocr_text="",
        combined_text="",
    )
    assert "qr" in files
    assert Path(files["qr"]).exists()
    qr_content = json.loads(Path(files["qr"]).read_text(encoding="utf-8"))
    assert qr_content["detectado"] is True


def test_write_debug_text_files_skips_qr_when_not_detected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="ccddeeff" + "2" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )
    files = write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text="",
        ocr_text="",
        combined_text="",
    )
    assert "qr" not in files


def test_write_debug_text_files_write_error_does_not_raise(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="ddeeff00" + "3" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )

    def fake_write(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with patch("invoice_parser_helpers._atomic_text_write", side_effect=fake_write):
        files = write_debug_text_files(
            invoice=invoice,
            output_dir=str(tmp_path),
            pdf_text="content",
            ocr_text="",
            combined_text="",
        )
    assert "pdf_text" not in files


def test_write_debug_text_files_invoice_has_debug_files_in_diagnostico(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="eeff0011" + "4" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )
    files = write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text="pdf content",
        ocr_text="ocr content",
        combined_text="combined content",
    )
    assert files
    diag = invoice.get("diagnostico", {})
    debug_files = diag.get("debug_files", {})
    assert debug_files.get("pdf_text") == files.get("pdf_text")
    assert debug_files.get("ocr_text") == files.get("ocr_text")


def test_write_debug_text_files_disabled_no_debug_files_in_invoice(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INVOICE_WRITE_DEBUG_TEXTS", raising=False)
    invoice = build_invoice_json(
        ocr_text=SAMPLE_TEXT,
        source_type="email",
        original_filename="factura.pdf",
        mime_type="application/pdf",
        sha256="ff001122" + "5" * 56,
        ocr_engine="pdf_text",
        pdf_text=SAMPLE_TEXT,
    )
    write_debug_text_files(
        invoice=invoice,
        output_dir=str(tmp_path),
        pdf_text="",
        ocr_text="",
        combined_text="",
    )
    diag = invoice.get("diagnostico", {})
    assert "debug_files" not in diag


def test_enqueue_invoice_job_preserves_ready_file_with_debug_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("INVOICE_WRITE_DEBUG_TEXTS", "true")
    (tmp_path / "pendientes").mkdir(parents=True)
    (tmp_path / "procesados").mkdir(parents=True)
    (tmp_path / "errores").mkdir(parents=True)
    result = service.enqueue_invoice_job(
        data=b"new content",
        source_type="email",
        original_filename="test.pdf",
        mime_type="application/pdf",
        source_metadata={},
    )
    assert result["status"] == "QUEUED"
