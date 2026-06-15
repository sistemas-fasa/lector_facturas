#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-/tmp/lector_factura.env}"
REMOTE_ENV="$N8N_DIR/.env"

echo "== aplicar variables email invoice-parser desde .env local =="
echo "N8N_DIR=$N8N_DIR"

if [ ! -f "$LOCAL_ENV_FILE" ]; then
  echo "ERROR: no existe $LOCAL_ENV_FILE" >&2
  exit 1
fi

mkdir -p "$N8N_DIR"
touch "$REMOTE_ENV"
cp "$REMOTE_ENV" "$REMOTE_ENV.bak-$(date +%Y%m%d-%H%M%S)"

python3 - <<'PY'
import os
from pathlib import Path

src = Path("/tmp/lector_factura.env")
dst = Path(os.environ.get("N8N_DIR", "/home/ferreteria/n8n")) / ".env"
keys = [
    "INVOICE_EMAIL_IMAP_POLL_ENABLED",
    "INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS",
    "INVOICE_EMAIL_IMAP_HOST",
    "INVOICE_EMAIL_IMAP_PORT",
    "INVOICE_EMAIL_IMAP_USER",
    "INVOICE_EMAIL_IMAP_PASSWORD",
    "INVOICE_EMAIL_FOLDER",
    "INVOICE_EMAIL_ALLOWED_EXTENSIONS",
    "INVOICE_EMAIL_ALLOWED_SENDERS",
    "INVOICE_QR_MAX_PAGES",
    "INVOICE_QR_ZBAR_TIMEOUT_SECONDS",
    "INVOICE_QR_MAX_VARIANTS",
]

def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data

incoming = parse_env(src)
current = parse_env(dst)
for key in keys:
    if key in incoming:
        current[key] = incoming[key]

lines = [f"{key}={value}" for key, value in current.items()]
dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("variables_actualizadas=" + ",".join(k for k in keys if k in incoming))
PY

cd "$N8N_DIR"
docker compose config >/tmp/n8n-compose-email-env-check.yml
docker compose up -d --no-deps invoice-parser

echo "== health invoice-parser =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'
echo
echo "OK: variables email aplicadas al invoice-parser"
