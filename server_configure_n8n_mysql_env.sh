#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
ENV_FILE="$N8N_DIR/.env"
BACKUP_FILE="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"

echo "== configurar variables MySQL para invoice-parser =="
echo "N8N_DIR=$N8N_DIR"

mkdir -p "$N8N_DIR"
touch "$ENV_FILE"
cp "$ENV_FILE" "$BACKUP_FILE"
echo "Backup: $BACKUP_FILE"

current_value() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true
}

ask_value() {
  local key="$1"
  local label="$2"
  local default_value="$3"
  local current
  current="$(current_value "$key")"
  if [ -n "$current" ]; then
    default_value="$current"
  fi
  read -r -p "$label [$default_value]: " value
  printf '%s' "${value:-$default_value}"
}

ask_secret() {
  local key="$1"
  local label="$2"
  local current
  current="$(current_value "$key")"
  if [ -n "$current" ]; then
    read -r -s -p "$label [ENTER para conservar actual]: " value
  else
    read -r -s -p "$label: " value
  fi
  echo
  if [ -z "${value:-}" ] && [ -n "$current" ]; then
    printf '%s' "$current"
  else
    printf '%s' "${value:-}"
  fi
}

env_quote() {
  local value="$1"
  printf '%s' "$value"
}

HOST="$(ask_value FASA_MYSQL_HOST 'Host MySQL visible desde Docker' 'host.docker.internal')"
PORT="$(ask_value FASA_MYSQL_PORT 'Puerto MySQL' '3306')"
DATABASE="$(ask_value FASA_MYSQL_DATABASE 'Base MySQL' 'fasa')"
USER="$(ask_value FASA_MYSQL_USER 'Usuario MySQL' '')"
PASSWORD="$(ask_secret FASA_MYSQL_PASSWORD 'Password MySQL')"

if [ -z "$HOST" ] || [ -z "$PORT" ] || [ -z "$DATABASE" ] || [ -z "$USER" ] || [ -z "$PASSWORD" ]; then
  echo "ERROR: faltan datos obligatorios. No se actualizo $ENV_FILE" >&2
  exit 1
fi

tmp="$(mktemp)"
grep -E '^([A-Za-z_][A-Za-z0-9_]*=|#|$)' "$ENV_FILE" \
  | grep -Ev '^(FASA_MYSQL_HOST|FASA_MYSQL_PORT|FASA_MYSQL_DATABASE|FASA_MYSQL_USER|FASA_MYSQL_PASSWORD)=' > "$tmp" || true
{
  cat "$tmp"
  echo "FASA_MYSQL_HOST=$(env_quote "$HOST")"
  echo "FASA_MYSQL_PORT=$(env_quote "$PORT")"
  echo "FASA_MYSQL_DATABASE=$(env_quote "$DATABASE")"
  echo "FASA_MYSQL_USER=$(env_quote "$USER")"
  echo "FASA_MYSQL_PASSWORD=$(env_quote "$PASSWORD")"
} > "$ENV_FILE"
rm -f "$tmp"

chmod 600 "$ENV_FILE" || true

echo "== variables guardadas =="
grep -E '^(FASA_MYSQL_HOST|FASA_MYSQL_PORT|FASA_MYSQL_DATABASE|FASA_MYSQL_USER)=' "$ENV_FILE"
echo "FASA_MYSQL_PASSWORD=***"

cd "$N8N_DIR"

echo "== probar puerto MySQL desde servidor =="
if command -v nc >/dev/null 2>&1; then
  nc -z -w 5 "$HOST" "$PORT" && echo "OK: puerto $HOST:$PORT accesible desde servidor" || echo "AVISO: no se pudo abrir $HOST:$PORT desde servidor"
elif command -v timeout >/dev/null 2>&1; then
  timeout 5 bash -c "cat < /dev/null > /dev/tcp/$HOST/$PORT" && echo "OK: puerto $HOST:$PORT accesible desde servidor" || echo "AVISO: no se pudo abrir $HOST:$PORT desde servidor"
fi

if docker compose config >/tmp/n8n-compose-check.yml; then
  echo "== reiniciar sidecar invoice-parser con nuevas variables =="
  docker compose up -d --force-recreate --no-deps invoice-parser
else
  echo "AVISO: docker compose config fallo. Ejecuta primero el setup del sidecar." >&2
  exit 1
fi

echo "== verificar variables dentro del sidecar =="
docker exec -i invoice-parser python - <<'PY'
import os
keys = ["FASA_MYSQL_HOST", "FASA_MYSQL_PORT", "FASA_MYSQL_DATABASE", "FASA_MYSQL_USER", "FASA_MYSQL_PASSWORD"]
for key in keys:
    value = os.environ.get(key, "")
    print(f"{key}={'***' if key.endswith('PASSWORD') and value else value}")
PY

echo "== probar MySQL desde invoice-parser =="
docker exec -i invoice-parser python - <<'PY'
import os, pymysql
try:
    conn = pymysql.connect(
        host=os.environ["FASA_MYSQL_HOST"],
        port=int(os.environ.get("FASA_MYSQL_PORT") or 3306),
        user=os.environ["FASA_MYSQL_USER"],
        password=os.environ["FASA_MYSQL_PASSWORD"],
        database=os.environ.get("FASA_MYSQL_DATABASE") or "fasa",
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        charset="utf8mb4",
    )
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM proveedo")
            print("OK: MySQL accesible desde invoice-parser, proveedo rows=", cur.fetchone()[0])
except Exception as exc:
    raise SystemExit(f"ERROR: MySQL no accesible desde invoice-parser: {exc}")
PY

echo "OK: variables MySQL cargadas en invoice-parser"
