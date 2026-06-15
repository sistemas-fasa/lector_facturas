"""Batch-test the n8n invoice webhook and summarize parsed fields."""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib import error, request

from create_n8n_invoice_parser_vfp import N8nClient, load_config


WORKFLOW_ID = "bNcDECjliKH5U2Hh"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=r"C:\fe\facturas", help="Folder with PDF/JPG/PNG invoices")
    parser.add_argument("--glob", default="*.pdf", help="File glob inside --dir")
    parser.add_argument("--limit", type=int, default=10, help="Maximum files to upload")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many matching files")
    parser.add_argument("--url", default="", help="Webhook URL override")
    parser.add_argument("--keep-active", action="store_true", help="Do not deactivate workflow after test")
    parser.add_argument("--report-dir", default="batch_reports")
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob(args.glob))[args.offset :]
    files = [p for p in files if p.is_file()][: args.limit]
    if not files:
        raise SystemExit(f"No encontre archivos con {Path(args.dir) / args.glob}")

    cfg = load_config()
    url = args.url or cfg["N8N_API_URL"].rstrip("/") + "/webhook/facturas/vfp-parser"
    client = N8nClient(cfg["N8N_API_URL"], cfg["N8N_API_KEY"])
    workflow = client.request("GET", f"/api/v1/workflows/{WORKFLOW_ID}")
    was_active = bool(workflow.get("active"))

    rows: list[dict[str, object]] = []
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    try:
        if not was_active:
            client.request("POST", f"/api/v1/workflows/{WORKFLOW_ID}/activate")
            print("workflow activated")

        for index, path in enumerate(files, start=1):
            print(f"[{index}/{len(files)}] {path}")
            row = upload_and_collect(path, url)
            rows.append(row)
            print(
                "  ->",
                row.get("status"),
                row.get("letra"),
                row.get("numero"),
                row.get("emisor_razon_social"),
                row.get("total"),
                "cuenta=" + str(row.get("cuenta_contable") or ""),
                "score=" + str(row.get("score_sugerencia") or ""),
                "review=" + str(row.get("requiere_revision")),
                "staging=" + str(row.get("staging_ok")) if "staging_ok" in row else "staging=n/a",
            )
    finally:
        if not was_active and not args.keep_active:
            client.request("POST", f"/api/v1/workflows/{WORKFLOW_ID}/deactivate")
            print("workflow deactivated")

    json_report = report_dir / f"invoice_batch_{stamp}.json"
    csv_report = report_dir / f"invoice_batch_{stamp}.csv"
    json_report.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_report, rows)
    print(f"json_report={json_report.resolve()}")
    print(f"csv_report={csv_report.resolve()}")
    print_summary(rows)
    return 0 if all(row.get("status") == "OK" for row in rows) else 1


def upload_and_collect(path: Path, url: str) -> dict[str, object]:
    started = time.time()
    row: dict[str, object] = {
        "file": str(path),
        "size": path.stat().st_size,
        "http_status": None,
        "status": "ERROR",
        "error": "",
    }
    try:
        code, payload = upload_file(path, url)
        row["http_status"] = code
        row.update({f"response_{k}": v for k, v in payload.items()})
        staging = payload.get("staging")
        if isinstance(staging, dict):
            row.update(
                {
                    "staging_enabled": staging.get("enabled"),
                    "staging_ok": staging.get("ok"),
                    "staging_factura_id": staging.get("factura_id"),
                    "staging_detalle_rows": staging.get("detalle_rows"),
                    "staging_error": staging.get("error"),
                }
            )
        row["status"] = payload.get("status", "UNKNOWN")
        json_file = payload.get("json_file")
        if json_file:
            parsed = read_remote_invoice_json(str(json_file))
            row.update(flatten_invoice(parsed))
    except Exception as exc:
        row["error"] = str(exc)
    row["elapsed_seconds"] = round(time.time() - started, 3)
    return row


def upload_file(path: Path, url: str) -> tuple[int, dict[str, object]]:
    boundary = "----n8nInvoiceBoundary" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = build_multipart(boundary, "file", path.name, mime, path.read_bytes())
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    try:
        with request.urlopen(req, timeout=180) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(text) if text.strip() else {}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}") from exc


def build_multipart(boundary: str, field: str, filename: str, mime: str, data: bytes) -> bytes:
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )


def read_remote_invoice_json(container_path: str) -> dict[str, object]:
    host_path = container_path.replace("/var/data/facturas_parseadas", "/home/ferreteria/n8n/data/facturas_parseadas")
    command = (
        "python3 - <<'PY'\n"
        "import json\n"
        "from pathlib import Path\n"
        f"p=Path({host_path!r})\n"
        "print(p.read_text())\n"
        "PY"
    )
    result = subprocess.run(["ssh", "fasa_195", command], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def flatten_invoice(data: dict[str, object]) -> dict[str, object]:
    comprobante = data.get("comprobante", {}) or {}
    emisor = data.get("emisor", {}) or {}
    receptor = data.get("receptor", {}) or {}
    importes = data.get("importes", {}) or {}
    validaciones = data.get("validaciones", {}) or {}
    contabilidad = data.get("contabilidad", {}) or {}
    return {
        "estado_json": data.get("estado"),
        "letra": comprobante.get("letra"),
        "punto_venta": comprobante.get("punto_venta"),
        "numero": comprobante.get("numero"),
        "fecha_emision": comprobante.get("fecha_emision"),
        "fecha_vencimiento": comprobante.get("fecha_vencimiento"),
        "moneda": comprobante.get("moneda"),
        "cae": comprobante.get("cae"),
        "cae_vencimiento": comprobante.get("cae_vencimiento"),
        "emisor_razon_social": emisor.get("razon_social"),
        "emisor_cuit": emisor.get("cuit"),
        "emisor_iva": emisor.get("iva_condicion"),
        "receptor_razon_social": receptor.get("razon_social"),
        "receptor_cuit": receptor.get("cuit"),
        "total": importes.get("total"),
        "proveedor_codigo": contabilidad.get("proveedor_codigo"),
        "cuenta_contable": contabilidad.get("cuenta_contable"),
        "cuenta_descripcion": contabilidad.get("cuenta_descripcion"),
        "origen_sugerencia": contabilidad.get("origen_sugerencia"),
        "score_sugerencia": contabilidad.get("score_sugerencia"),
        "requiere_confirmacion_contable": contabilidad.get("requiere_confirmacion"),
        "contabilidad_observaciones": "; ".join(contabilidad.get("observaciones", []) or []),
        "requiere_revision": validaciones.get("requiere_revision"),
        "observaciones": "; ".join(validaciones.get("observaciones", []) or []),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: list[dict[str, object]]) -> None:
    print("summary:")
    print(f"  total={len(rows)}")
    print(f"  ok={sum(1 for row in rows if row.get('status') == 'OK')}")
    print(f"  error={sum(1 for row in rows if row.get('status') != 'OK')}")
    print(f"  review={sum(1 for row in rows if row.get('requiere_revision') is True)}")
    staging_rows = [row for row in rows if "staging_ok" in row]
    if staging_rows:
        print(f"  staging_ok={sum(1 for row in staging_rows if row.get('staging_ok') is True)}/{len(staging_rows)}")
        errors = [str(row.get("staging_error") or "") for row in staging_rows if row.get("staging_error")]
        if errors:
            print(f"  staging_errors={errors[:3]}")
    issuers = sorted({str(row.get("emisor_razon_social") or "") for row in rows if row.get("emisor_razon_social")})
    print(f"  issuers={issuers}")


if __name__ == "__main__":
    raise SystemExit(main())
