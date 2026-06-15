#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"

echo "== diagnostico runtime MySQL invoice-parser =="
echo "N8N_DIR=$N8N_DIR"

cd "$N8N_DIR"

echo "== docker ps =="
docker ps --filter name=invoice-parser --filter name=n8n --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

echo "== compose env efectivo para invoice-parser =="
docker compose config | sed -n '/^[[:space:]]*invoice-parser:/,/^[[:space:]]*[a-zA-Z0-9_-]*:/p' | sed -n '/environment:/,/volumes:/p' | sed 's/\(FASA_MYSQL_PASSWORD: \).*/\1***/'

echo "== env visto por docker exec =="
docker exec -i invoice-parser python - <<'PY'
import os
for key in ["FASA_MYSQL_HOST","FASA_MYSQL_PORT","FASA_MYSQL_DATABASE","FASA_MYSQL_USER","FASA_MYSQL_PASSWORD"]:
    value = os.environ.get(key, "")
    print(f"{key}={'***' if key.endswith('PASSWORD') and value else value} present={bool(value)}")
PY

echo "== env del proceso PID 1 dentro del container =="
docker exec invoice-parser sh -lc "tr '\0' '\n' < /proc/1/environ | grep '^FASA_MYSQL_' | while IFS= read -r line; do case \"\$line\" in FASA_MYSQL_PASSWORD=*) value=\${line#FASA_MYSQL_PASSWORD=}; if [ -n \"\$value\" ]; then echo 'FASA_MYSQL_PASSWORD=*** present=true'; else echo 'FASA_MYSQL_PASSWORD= present=false'; fi ;; *) echo \"\$line\" ;; esac; done || true"

echo "== health local dentro de invoice-parser =="
docker exec -i invoice-parser python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5).read().decode())
PY

echo "== health desde n8n =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'
echo

echo "== test directo de parse con factura original existente si hay una =="
sample="$(find "$N8N_DIR/data/facturas_parseadas/originales" -type f \( -name '*.pdf' -o -name '*.PDF' \) | head -n 1 || true)"
if [ -n "$sample" ]; then
  echo "sample=$sample"
  docker exec -i invoice-parser python - "$sample" <<'PY'
import json
import sys
from pathlib import Path
from urllib import request

host_path = Path(sys.argv[1])
container_path = str(host_path).replace("/home/ferreteria/n8n/data/facturas_parseadas", "/var/data/facturas_parseadas")
data = Path(container_path).read_bytes()
boundary = "----diag"
body = b"".join([
    f"--{boundary}\r\n".encode(),
    b'Content-Disposition: form-data; name="file"; filename="diag.pdf"\r\n',
    b"Content-Type: application/pdf\r\n\r\n",
    data,
    b"\r\n",
    f"--{boundary}--\r\n".encode(),
])
req = request.Request("http://127.0.0.1:8765/parse?source_type=diagnose", data=body, method="POST")
req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
resp = json.loads(request.urlopen(req, timeout=60).read().decode())
print(json.dumps(resp, indent=2, ensure_ascii=False))
if resp.get("json_file"):
    p = Path(resp["json_file"])
    parsed = json.loads(p.read_text())
    print("contabilidad=", json.dumps(parsed.get("contabilidad"), ensure_ascii=False))
PY
else
  echo "sin originales pdf para test directo"
fi

echo "OK: diagnostico runtime finalizado"
