#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
COMPOSE_FILE="$N8N_DIR/docker-compose.yml"
BACKUP_FILE="$N8N_DIR/docker-compose.yml.bak-$(date +%Y%m%d-%H%M%S)"
PARSER_DIR="$N8N_DIR/invoice_parser"

echo "== n8n invoice parser sidecar setup =="
echo "N8N_DIR=$N8N_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "ERROR: no existe $COMPOSE_FILE" >&2
  exit 1
fi

if [ ! -f /tmp/host_invoice_parser_service.py ] || [ ! -f /tmp/invoice_parser_helpers.py ] || [ ! -d /tmp/factura_ocr ]; then
  echo "ERROR: faltan /tmp/host_invoice_parser_service.py, /tmp/invoice_parser_helpers.py o /tmp/factura_ocr" >&2
  exit 1
fi

cd "$N8N_DIR"

echo "== backup compose =="
cp "$COMPOSE_FILE" "$BACKUP_FILE"
echo "Backup: $BACKUP_FILE"

echo "== preparar servicio parser =="
mkdir -p "$PARSER_DIR"
cp /tmp/host_invoice_parser_service.py /tmp/invoice_parser_helpers.py "$PARSER_DIR/"
rm -rf "$PARSER_DIR/factura_ocr"
cp -R /tmp/factura_ocr "$PARSER_DIR/"

cat > "$PARSER_DIR/Dockerfile" <<'EOF'
FROM python:3.12-alpine

RUN apk add --no-cache poppler-utils tesseract-ocr tesseract-ocr-data-spa zbar zbar-dev
RUN pip install --no-cache-dir pymupdf pillow pytesseract pymysql

WORKDIR /app
COPY host_invoice_parser_service.py invoice_parser_helpers.py ./
COPY factura_ocr ./factura_ocr

ENV INVOICE_HELPER_HOST=0.0.0.0
ENV INVOICE_HELPER_PORT=8765
ENV INVOICE_OUTPUT_DIR=/var/data/facturas_parseadas
ENV INVOICE_GENERATE_XML=true
ENV FASA_MYSQL_HOST=
ENV FASA_MYSQL_PORT=3306
ENV FASA_MYSQL_DATABASE=fasa
ENV FASA_MYSQL_USER=
ENV FASA_MYSQL_PASSWORD=
ENV INVOICE_QR_MAX_PAGES=2
ENV INVOICE_QR_ZBAR_TIMEOUT_SECONDS=2
ENV INVOICE_QR_MAX_VARIANTS=10
ENV INVOICE_EMAIL_IMAP_POLL_ENABLED=false
ENV INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS=60

EXPOSE 8765
CMD ["python", "host_invoice_parser_service.py"]
EOF

echo "== actualizar docker-compose.yml =="
python3 - <<'PY'
from pathlib import Path

p = Path("docker-compose.yml")
s = p.read_text()

# Repair a previous failed attempt that changed n8n into a custom build.
if "Dockerfile.invoice-parser" in s or "n8n-invoice-parser:local" in s:
    start = s.index("  n8n:\n")
    next_service = s.find("\n  invoice-parser:", start + 1)
    if next_service == -1:
        n8n_block = """  n8n:
    image: n8nio/n8n
    container_name: n8n
    restart: unless-stopped
    ports:
      - "127.0.0.1:5678:5678"
    environment:
      - N8N_HOST=n8n.vogelconsultoria.com.ar
      - N8N_PROTOCOL=https
      - N8N_PORT=5678
      - WEBHOOK_URL=https://n8n.vogelconsultoria.com.ar
      - N8N_METRICS=false
      - N8N_METRICS_INCLUDE_DEFAULT_METRICS=true
    volumes:
      - ./data:/home/node/.n8n
"""
        s = s[:start] + n8n_block

if "  invoice-parser:" not in s:
    s = s.rstrip() + """

  invoice-parser:
    build:
      context: ./invoice_parser
    container_name: invoice-parser
    restart: unless-stopped
    expose:
      - "8765"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - FASA_MYSQL_HOST=${FASA_MYSQL_HOST:-}
      - FASA_MYSQL_PORT=${FASA_MYSQL_PORT:-3306}
      - FASA_MYSQL_DATABASE=${FASA_MYSQL_DATABASE:-fasa}
      - FASA_MYSQL_USER=${FASA_MYSQL_USER:-}
      - FASA_MYSQL_PASSWORD=${FASA_MYSQL_PASSWORD:-}
      - INVOICE_QR_MAX_PAGES=${INVOICE_QR_MAX_PAGES:-2}
      - INVOICE_QR_ZBAR_TIMEOUT_SECONDS=${INVOICE_QR_ZBAR_TIMEOUT_SECONDS:-2}
      - INVOICE_QR_MAX_VARIANTS=${INVOICE_QR_MAX_VARIANTS:-10}
      - INVOICE_EMAIL_IMAP_POLL_ENABLED=${INVOICE_EMAIL_IMAP_POLL_ENABLED:-false}
      - INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS=${INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS:-60}
      - INVOICE_EMAIL_IMAP_HOST=${INVOICE_EMAIL_IMAP_HOST:-}
      - INVOICE_EMAIL_IMAP_PORT=${INVOICE_EMAIL_IMAP_PORT:-993}
      - INVOICE_EMAIL_IMAP_USER=${INVOICE_EMAIL_IMAP_USER:-}
      - INVOICE_EMAIL_IMAP_PASSWORD=${INVOICE_EMAIL_IMAP_PASSWORD:-}
      - INVOICE_EMAIL_FOLDER=${INVOICE_EMAIL_FOLDER:-INBOX}
      - INVOICE_EMAIL_ALLOWED_EXTENSIONS=${INVOICE_EMAIL_ALLOWED_EXTENSIONS:-pdf,jpg,jpeg,png}
      - INVOICE_EMAIL_ALLOWED_SENDERS=${INVOICE_EMAIL_ALLOWED_SENDERS:-}
    volumes:
      - ./data/facturas_parseadas:/var/data/facturas_parseadas
"""
elif "FASA_MYSQL_HOST" not in s:
    marker = '    expose:\n      - "8765"\n'
    env_block = """    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - FASA_MYSQL_HOST=${FASA_MYSQL_HOST:-}
      - FASA_MYSQL_PORT=${FASA_MYSQL_PORT:-3306}
      - FASA_MYSQL_DATABASE=${FASA_MYSQL_DATABASE:-fasa}
      - FASA_MYSQL_USER=${FASA_MYSQL_USER:-}
      - FASA_MYSQL_PASSWORD=${FASA_MYSQL_PASSWORD:-}
"""
    start = s.index("  invoice-parser:\n")
    insert_at = s.find(marker, start)
    if insert_at != -1:
        insert_at += len(marker)
        s = s[:insert_at] + env_block + s[insert_at:]
elif "host.docker.internal:host-gateway" not in s:
    marker = '    expose:\n      - "8765"\n'
    extra_hosts_block = """    extra_hosts:
      - "host.docker.internal:host-gateway"
"""
    start = s.index("  invoice-parser:\n")
    insert_at = s.find(marker, start)
    if insert_at != -1:
        insert_at += len(marker)
        s = s[:insert_at] + extra_hosts_block + s[insert_at:]

if "INVOICE_QR_MAX_PAGES" not in s and "  invoice-parser:" in s:
    marker = "      - FASA_MYSQL_PASSWORD=${FASA_MYSQL_PASSWORD:-}\n"
    start = s.index("  invoice-parser:\n")
    insert_at = s.find(marker, start)
    if insert_at != -1:
        insert_at += len(marker)
        s = s[:insert_at] + """      - INVOICE_QR_MAX_PAGES=${INVOICE_QR_MAX_PAGES:-2}
      - INVOICE_QR_ZBAR_TIMEOUT_SECONDS=${INVOICE_QR_ZBAR_TIMEOUT_SECONDS:-2}
      - INVOICE_QR_MAX_VARIANTS=${INVOICE_QR_MAX_VARIANTS:-10}
""" + s[insert_at:]

if "INVOICE_EMAIL_IMAP_POLL_ENABLED" not in s and "  invoice-parser:" in s:
    marker = "      - INVOICE_QR_MAX_VARIANTS=${INVOICE_QR_MAX_VARIANTS:-10}\n"
    start = s.index("  invoice-parser:\n")
    insert_at = s.find(marker, start)
    if insert_at != -1:
        insert_at += len(marker)
        s = s[:insert_at] + """      - INVOICE_EMAIL_IMAP_POLL_ENABLED=${INVOICE_EMAIL_IMAP_POLL_ENABLED:-false}
      - INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS=${INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS:-60}
      - INVOICE_EMAIL_IMAP_HOST=${INVOICE_EMAIL_IMAP_HOST:-}
      - INVOICE_EMAIL_IMAP_PORT=${INVOICE_EMAIL_IMAP_PORT:-993}
      - INVOICE_EMAIL_IMAP_USER=${INVOICE_EMAIL_IMAP_USER:-}
      - INVOICE_EMAIL_IMAP_PASSWORD=${INVOICE_EMAIL_IMAP_PASSWORD:-}
      - INVOICE_EMAIL_FOLDER=${INVOICE_EMAIL_FOLDER:-INBOX}
      - INVOICE_EMAIL_ALLOWED_EXTENSIONS=${INVOICE_EMAIL_ALLOWED_EXTENSIONS:-pdf,jpg,jpeg,png}
      - INVOICE_EMAIL_ALLOWED_SENDERS=${INVOICE_EMAIL_ALLOWED_SENDERS:-}
""" + s[insert_at:]

p.write_text(s)
PY

echo "== preparar carpeta de salida =="
mkdir -p data/facturas_parseadas/{originales,errores,duplicados,procesados}
chown -R 1000:1000 data/facturas_parseadas

echo "== validar compose =="
docker compose config >/tmp/n8n-compose-check.yml

echo "== construir/levantar sidecar sin recrear n8n =="
docker compose up -d --build invoice-parser

echo "== verificar sidecar =="
docker exec invoice-parser sh -lc 'command -v tesseract && command -v pdftotext'
docker exec invoice-parser sh -lc 'command -v zbarimg'
docker exec invoice-parser sh -lc 'ls -l /usr/lib/libzbar* || true'
docker exec -i invoice-parser python - <<'PY'
import factura_ocr.extract
import pymysql
from PIL import Image
print("facturas_ocr deps OK")
PY

echo "== esperar healthcheck del sidecar =="
ok=0
for i in $(seq 1 20); do
  if docker exec -i invoice-parser python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2).read().decode())
PY
  then
    ok=1
    break
  fi
  echo "healthcheck intento $i/20 aun no responde"
  docker ps -a --filter name=invoice-parser --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
  sleep 1
done

if [ "$ok" != "1" ]; then
  echo "ERROR: invoice-parser no respondio healthcheck" >&2
  echo "== logs invoice-parser =="
  docker logs --tail=120 invoice-parser || true
  exit 1
fi

echo "== verificar alcance desde n8n al sidecar =="
docker exec n8n sh -lc 'for i in $(seq 1 10); do timeout 5 wget -qO- http://invoice-parser:8765/health && exit 0; sleep 1; done; exit 1'

echo "== estado =="
docker ps --filter name=n8n --filter name=invoice-parser --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
echo "OK: invoice-parser sidecar listo para probar desde n8n"
