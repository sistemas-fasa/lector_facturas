"""Local review server for testing invoice parsing through n8n.

Open http://127.0.0.1:8791, upload an invoice, and review the final JSON.
The server temporarily activates the n8n workflow if needed, then restores it.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import subprocess
import tempfile
import time
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from create_n8n_invoice_parser_vfp import N8nClient, load_config


HOST = os.environ.get("INVOICE_REVIEW_HOST", "127.0.0.1")
PORT = int(os.environ.get("INVOICE_REVIEW_PORT", "8791"))
WORKFLOW_ID = os.environ.get("INVOICE_WORKFLOW_ID", "bNcDECjliKH5U2Hh")
REMOTE_OUTPUT_PREFIX = "/var/data/facturas_parseadas"
HOST_OUTPUT_PREFIX = "/home/ferreteria/n8n/data/facturas_parseadas"


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceReview/1.0"

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            self.send_html(index_page())
            return
        if self.path == "/health":
            self.send_json({"status": "OK"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/api/upload":
            self.send_error(404)
            return
        try:
            upload = self.read_upload()
            result = process_upload(upload["filename"], upload["mime_type"], upload["data"])
            self.send_json(result)
        except Exception as exc:
            self.send_json({"status": "ERROR", "error": str(exc)}, status=500)

    def read_upload(self) -> dict[str, object]:
        content_type = self.headers.get("content-type", "")
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        if not body:
            raise ValueError("El request no tiene cuerpo")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
        )
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = Path(part.get_filename() or "factura.bin").name
            data = part.get_payload(decode=True) or b""
            if not data:
                raise ValueError("El archivo subido esta vacio")
            return {
                "filename": filename,
                "mime_type": part.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "data": data,
            }
        raise ValueError("Subi un archivo en el campo file")

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def process_upload(filename: str, mime_type: str, data: bytes) -> dict[str, object]:
    cfg = load_config()
    if not cfg["N8N_API_URL"] or not cfg["N8N_API_KEY"]:
        raise RuntimeError("Faltan N8N_API_URL o N8N_API_KEY en .env")

    client = N8nClient(cfg["N8N_API_URL"], cfg["N8N_API_KEY"])
    workflow = client.request("GET", f"/api/v1/workflows/{WORKFLOW_ID}")
    was_active = bool(workflow.get("active"))
    webhook_url = cfg["N8N_API_URL"].rstrip("/") + "/webhook/facturas/vfp-parser"
    started = time.time()

    try:
        if not was_active:
            client.request("POST", f"/api/v1/workflows/{WORKFLOW_ID}/activate")
        webhook_response = upload_to_webhook(webhook_url, filename, mime_type, data)
    finally:
        if not was_active:
            client.request("POST", f"/api/v1/workflows/{WORKFLOW_ID}/deactivate")

    invoice_json = None
    json_file = webhook_response.get("json_file")
    if isinstance(json_file, str) and json_file:
        invoice_json = read_remote_json(json_file)

    return {
        "status": webhook_response.get("status", "UNKNOWN"),
        "elapsed_seconds": round(time.time() - started, 3),
        "workflow_restored_active": was_active,
        "webhook_response": webhook_response,
        "invoice_json": invoice_json,
    }


def upload_to_webhook(url: str, filename: str, mime_type: str, data: bytes) -> dict[str, object]:
    boundary = "----invoiceReviewBoundary" + uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    try:
        with request.urlopen(req, timeout=180) as response:
            text = response.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {"status": "EMPTY_RESPONSE", "http_status": response.status}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"n8n webhook HTTP {exc.code}: {text}") from exc


def read_remote_json(container_path: str) -> dict[str, object]:
    host_path = container_path.replace(REMOTE_OUTPUT_PREFIX, HOST_OUTPUT_PREFIX)
    command = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"p = Path({host_path!r})\n"
        "print(p.read_text())\n"
        "PY"
    )
    result = subprocess.run(["ssh", "fasa_195", command], text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def index_page() -> str:
    return """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prueba Parser Facturas</title>
  <style>
    :root { color-scheme: light; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #17202a; }
    main { max-width: 980px; margin: 32px auto; padding: 0 20px; }
    h1 { font-size: 24px; margin: 0 0 18px; }
    form, section { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
    input[type=file] { display: block; margin: 12px 0 16px; }
    button { background: #155eef; color: white; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; }
    button:disabled { background: #8292aa; cursor: wait; }
    pre { white-space: pre-wrap; word-break: break-word; background: #101828; color: #eef4ff; border-radius: 8px; padding: 16px; min-height: 220px; }
    .status { font-weight: 700; margin-top: 10px; }
  </style>
</head>
<body>
<main>
  <h1>Prueba Parser Facturas</h1>
  <form id="form">
    <label>Factura PDF/JPG/PNG</label>
    <input id="file" name="file" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/*" required>
    <button id="btn" type="submit">Subir y parsear</button>
    <div id="status" class="status"></div>
  </form>
  <section>
    <pre id="out">Esperando archivo...</pre>
  </section>
</main>
<script>
const form = document.getElementById('form');
const btn = document.getElementById('btn');
const statusEl = document.getElementById('status');
const out = document.getElementById('out');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = document.getElementById('file').files[0];
  if (!file) return;
  btn.disabled = true;
  statusEl.textContent = 'Procesando...';
  out.textContent = '';
  const data = new FormData();
  data.append('file', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: data });
    const text = await res.text();
    let payload;
    try { payload = JSON.parse(text); } catch { payload = { raw: text }; }
    statusEl.textContent = res.ok ? 'Listo' : 'Error';
    out.textContent = JSON.stringify(payload.invoice_json || payload, null, 2);
  } catch (err) {
    statusEl.textContent = 'Error';
    out.textContent = String(err);
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="invoice-review-"):
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        print(f"Servidor de prueba: http://{HOST}:{PORT}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
