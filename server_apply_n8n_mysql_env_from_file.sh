#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
ENV_FILE="$N8N_DIR/.env"
INPUT_FILE="${1:-/tmp/invoice_parser_mysql.env}"
BACKUP_FILE="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"

echo "== aplicar variables MySQL desde archivo para invoice-parser =="
echo "N8N_DIR=$N8N_DIR"
echo "INPUT_FILE=$INPUT_FILE"

if [ ! -f "$INPUT_FILE" ]; then
  echo "ERROR: no existe $INPUT_FILE" >&2
  exit 1
fi

mkdir -p "$N8N_DIR"
touch "$ENV_FILE"
cp "$ENV_FILE" "$BACKUP_FILE"
echo "Backup: $BACKUP_FILE"

python3 - "$ENV_FILE" "$INPUT_FILE" <<'PY'
import re
import shlex
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
input_path = Path(sys.argv[2])
required = [
    "FASA_MYSQL_HOST",
    "FASA_MYSQL_PORT",
    "FASA_MYSQL_DATABASE",
    "FASA_MYSQL_USER",
    "FASA_MYSQL_PASSWORD",
]

def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key not in required:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                value = shlex.split(value)[0]
            except Exception:
                value = value[1:-1]
        values[key] = value
    return values

incoming = parse_env(input_path)
missing = [key for key in required if not incoming.get(key)]
if missing:
    raise SystemExit("ERROR: faltan variables obligatorias: " + ", ".join(missing))

valid_line = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=|#|$)")
drop = tuple(key + "=" for key in required)
kept: list[str] = []
for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not valid_line.match(raw):
        continue
    if raw.startswith(drop):
        continue
    kept.append(raw)

for key in required:
    kept.append(f"{key}={incoming[key]}")

env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
print("Variables aplicadas:", ", ".join(required))
PY

chmod 600 "$ENV_FILE" || true

echo "== variables guardadas =="
grep -E '^(FASA_MYSQL_HOST|FASA_MYSQL_PORT|FASA_MYSQL_DATABASE|FASA_MYSQL_USER)=' "$ENV_FILE"
echo "FASA_MYSQL_PASSWORD=***"

cd "$N8N_DIR"

echo "== validar docker compose =="
docker compose config >/tmp/n8n-compose-check.yml

echo "== recrear sidecar invoice-parser =="
docker compose up -d --force-recreate --no-deps invoice-parser

echo "== verificar variables dentro del sidecar =="
docker exec -i invoice-parser python - <<'PY'
import os
keys = ["FASA_MYSQL_HOST", "FASA_MYSQL_PORT", "FASA_MYSQL_DATABASE", "FASA_MYSQL_USER", "FASA_MYSQL_PASSWORD"]
for key in keys:
    value = os.environ.get(key, "")
    masked = "***" if key.endswith("PASSWORD") and value else value
    print(f"{key}={masked} present={bool(value)}")
missing = [key for key in keys if not os.environ.get(key)]
if missing:
    raise SystemExit("ERROR: faltan variables dentro del sidecar: " + ", ".join(missing))
PY

echo "== probar MySQL desde invoice-parser =="
docker exec -i invoice-parser python - <<'PY'
import os
import pymysql

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
PY

echo "== health desde n8n =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'
echo

echo "OK: variables MySQL aplicadas desde .env local"
