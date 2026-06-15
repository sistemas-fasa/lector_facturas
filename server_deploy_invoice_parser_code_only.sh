#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
PARSER_DIR="$N8N_DIR/invoice_parser"

echo "== deploy invoice-parser code only =="
echo "N8N_DIR=$N8N_DIR"

if [ ! -d "$PARSER_DIR" ]; then
  echo "ERROR: no existe $PARSER_DIR" >&2
  exit 1
fi
if [ ! -f /tmp/host_invoice_parser_service.py ] || [ ! -f /tmp/invoice_parser_helpers.py ] || [ ! -d /tmp/factura_ocr ]; then
  echo "ERROR: faltan archivos actualizados en /tmp" >&2
  exit 1
fi

cd "$N8N_DIR"

echo "== backup archivos actuales =="
stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PARSER_DIR/backups/$stamp"
cp "$PARSER_DIR/host_invoice_parser_service.py" "$PARSER_DIR/invoice_parser_helpers.py" "$PARSER_DIR/backups/$stamp/" 2>/dev/null || true
if [ -d "$PARSER_DIR/factura_ocr" ]; then
  cp -R "$PARSER_DIR/factura_ocr" "$PARSER_DIR/backups/$stamp/" 2>/dev/null || true
fi

echo "== copiar codigo nuevo =="
cp /tmp/host_invoice_parser_service.py /tmp/invoice_parser_helpers.py "$PARSER_DIR/"
rm -rf "$PARSER_DIR/factura_ocr"
cp -R /tmp/factura_ocr "$PARSER_DIR/"

echo "== validar compose =="
docker compose config >/tmp/n8n-compose-check.yml

echo "== reconstruir invoice-parser =="
docker compose up -d --build invoice-parser

echo "== verificar health local =="
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
  docker logs --tail=30 invoice-parser || true
  sleep 1
done

if [ "$ok" != "1" ]; then
  echo "ERROR: invoice-parser no respondio healthcheck" >&2
  echo "== ultimos logs invoice-parser ==" >&2
  docker logs --tail=120 invoice-parser >&2 || true
  exit 1
fi

echo "== verificar desde n8n =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'

echo "OK: invoice-parser actualizado"
