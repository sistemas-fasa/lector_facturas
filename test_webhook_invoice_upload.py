"""Small webhook smoke test for the n8n invoice parser."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import uuid
from pathlib import Path
from urllib import request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="PDF/JPG/PNG invoice sample to upload")
    parser.add_argument("--url", default=os.environ.get("INVOICE_WEBHOOK_URL", ""))
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("Falta --url o INVOICE_WEBHOOK_URL")

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"No existe el archivo: {path}")

    boundary = "----n8nInvoiceBoundary" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = build_multipart(boundary, "file", path.name, mime, path.read_bytes())
    req = request.Request(args.url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    with request.urlopen(req, timeout=120) as response:
        print(response.status)
        text = response.read().decode("utf-8", errors="replace")
        try:
            print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(text)
    return 0


def build_multipart(boundary: str, field: str, filename: str, mime: str, data: bytes) -> bytes:
    lines = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
