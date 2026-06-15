#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
ENV_FILE="$N8N_DIR/.env"

echo "== reparar .env n8n tras configuracion MySQL =="
echo "ENV_FILE=$ENV_FILE"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: no existe $ENV_FILE" >&2
  exit 1
fi

echo "== backups disponibles =="
ls -1t "$ENV_FILE".bak-* 2>/dev/null | head -5 || true

read -r -p "Restaurar ultimo backup antes de reconfigurar? [S/n]: " RESTORE
RESTORE="${RESTORE:-S}"
if [[ "$RESTORE" =~ ^[SsYy]$ ]]; then
  LAST_BACKUP="$(ls -1t "$ENV_FILE".bak-* 2>/dev/null | head -1 || true)"
  if [ -z "$LAST_BACKUP" ]; then
    echo "ERROR: no hay backups para restaurar" >&2
    exit 1
  fi
  cp "$LAST_BACKUP" "$ENV_FILE"
  echo "Restaurado: $LAST_BACKUP"
else
  cp "$ENV_FILE" "$ENV_FILE.broken-$(date +%Y%m%d-%H%M%S)"
  tmp="$(mktemp)"
  grep -E '^([A-Za-z_][A-Za-z0-9_]*=|#|$)' "$ENV_FILE" \
    | grep -Ev '^(FASA_MYSQL_HOST|FASA_MYSQL_PORT|FASA_MYSQL_DATABASE|FASA_MYSQL_USER|FASA_MYSQL_PASSWORD)=' > "$tmp" || true
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  echo "Limpieza aplicada sin restaurar backup"
fi

echo "== validar compose =="
cd "$N8N_DIR"
docker compose config >/tmp/n8n-compose-check.yml
echo "OK: .env vuelve a ser legible por docker compose"
