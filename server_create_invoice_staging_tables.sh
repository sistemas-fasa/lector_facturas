#!/usr/bin/env bash
set -euo pipefail

SQL_FILE="${SQL_FILE:-/tmp/facturas_ocr_staging.sql}"
DB_NAME_DEFAULT="${FASA_MYSQL_DATABASE:-fasa}"

echo "== crear tablas staging facturas OCR =="

if [ ! -f "$SQL_FILE" ]; then
  echo "ERROR: no existe $SQL_FILE" >&2
  exit 1
fi

read -r -p "Host MySQL [192.168.0.150]: " DB_HOST
DB_HOST="${DB_HOST:-192.168.0.150}"
read -r -p "Puerto MySQL [3306]: " DB_PORT
DB_PORT="${DB_PORT:-3306}"
read -r -p "Base MySQL [$DB_NAME_DEFAULT]: " DB_NAME
DB_NAME="${DB_NAME:-$DB_NAME_DEFAULT}"
read -r -p "Usuario MySQL con permisos CREATE/GRANT [$USER]: " DB_USER
DB_USER="${DB_USER:-$USER}"
read -r -s -p "Password MySQL para $DB_USER: " DB_PASS
echo

if [ -z "$DB_NAME" ] || [ -z "$DB_USER" ]; then
  echo "ERROR: base y usuario son obligatorios" >&2
  exit 1
fi

if command -v mysql >/dev/null 2>&1; then
  SQL_RUNNER="mysql"
  MYSQL=(mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER")
  if [ -n "${DB_PASS:-}" ]; then
    MYSQL+=("-p$DB_PASS")
  fi
else
  SQL_RUNNER="invoice-parser-python"
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: no se encontro mysql ni docker para ejecutar SQL." >&2
    exit 1
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx 'invoice-parser'; then
    echo "ERROR: no existe el container invoice-parser para usar PyMySQL como fallback." >&2
    echo "Ejecuta primero .\\run_server_setup_n8n_invoice_parser.ps1 o instala mysql-client." >&2
    exit 1
  fi
fi

run_sql_query() {
  local query="$1"
  if [ "$SQL_RUNNER" = "mysql" ]; then
    "${MYSQL[@]}" "$DB_NAME" -e "$query"
  else
    docker exec \
      -e DB_HOST="$DB_HOST" \
      -e DB_PORT="$DB_PORT" \
      -e DB_NAME="$DB_NAME" \
      -e DB_USER="$DB_USER" \
      -e DB_PASS="$DB_PASS" \
      -e SQL_TEXT="$query" \
      -i invoice-parser python - <<'PY'
import os
import pymysql

conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ.get("DB_PORT") or 3306),
    user=os.environ["DB_USER"],
    password=os.environ.get("DB_PASS") or "",
    database=os.environ["DB_NAME"],
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=5,
    read_timeout=15,
    write_timeout=15,
    autocommit=True,
)
with conn:
    with conn.cursor() as cur:
        for statement in [s.strip() for s in os.environ["SQL_TEXT"].split(";") if s.strip()]:
            cur.execute(statement)
            if cur.description:
                columns = [col[0] for col in cur.description]
                print("\t".join(columns))
                for row in cur.fetchall():
                    print("\t".join("" if row.get(col) is None else str(row.get(col)) for col in columns))
PY
  fi
}

run_sql_file() {
  local file="$1"
  if [ "$SQL_RUNNER" = "mysql" ]; then
    "${MYSQL[@]}" "$DB_NAME" < "$file"
  else
    docker cp "$file" invoice-parser:/tmp/facturas_ocr_staging.sql
    docker exec \
      -e DB_HOST="$DB_HOST" \
      -e DB_PORT="$DB_PORT" \
      -e DB_NAME="$DB_NAME" \
      -e DB_USER="$DB_USER" \
      -e DB_PASS="$DB_PASS" \
      -i invoice-parser python - <<'PY'
import os
import re
from pathlib import Path
import pymysql

def split_sql(script: str) -> list[str]:
    statements = []
    current = []
    quote = None
    escape = False
    for ch in script:
        current.append(ch)
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            continue
        if ch == ";":
            statement = "".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements

sql = Path("/tmp/facturas_ocr_staging.sql").read_text(encoding="utf-8")
sql = re.sub(r"(?m)^--.*$", "", sql)
conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ.get("DB_PORT") or 3306),
    user=os.environ["DB_USER"],
    password=os.environ.get("DB_PASS") or "",
    database=os.environ["DB_NAME"],
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=5,
    read_timeout=30,
    write_timeout=30,
    autocommit=True,
)
with conn:
    with conn.cursor() as cur:
        for statement in split_sql(sql):
            cur.execute(statement)
print("SQL file applied")
PY
  fi
}

echo "== verificar conexion =="
run_sql_query "SELECT DATABASE();" >/dev/null

echo "== ejecutar schema staging =="
run_sql_file "$SQL_FILE"

echo "== verificar tablas =="
run_sql_query "
SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('facturas_ocr_cabecera','facturas_ocr_detalle','facturas_ocr_percepciones_iibb','facturas_ocr_eventos','facturas_ocr_email_origen')
ORDER BY TABLE_NAME;
"

run_sql_query "
SELECT TABLE_NAME
FROM information_schema.VIEWS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('vw_facturas_ocr_pendientes','vw_facturas_ocr_detalle','vw_facturas_ocr_percepciones_iibb')
ORDER BY TABLE_NAME;
"

read -r -p "Usuario del parser para otorgar escritura staging [invoice_parser_ro, ENTER omite]: " PARSER_USER
PARSER_USER="${PARSER_USER:-invoice_parser_ro}"
if [ -n "$PARSER_USER" ]; then
  read -r -p "Host del usuario parser [%]: " PARSER_HOST
  PARSER_HOST="${PARSER_HOST:-%}"
  echo "== otorgar permisos staging a '$PARSER_USER'@'$PARSER_HOST' =="
  run_sql_query "
GRANT SELECT, INSERT, UPDATE ON \`$DB_NAME\`.facturas_ocr_cabecera TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`$DB_NAME\`.facturas_ocr_detalle TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`$DB_NAME\`.facturas_ocr_percepciones_iibb TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT, INSERT ON \`$DB_NAME\`.facturas_ocr_eventos TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT, INSERT, UPDATE ON \`$DB_NAME\`.facturas_ocr_email_origen TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_pendientes TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_detalle TO '$PARSER_USER'@'$PARSER_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_percepciones_iibb TO '$PARSER_USER'@'$PARSER_HOST';
FLUSH PRIVILEGES;
"
fi

read -r -p "Usuarios FoxPro existentes para otorgar permisos, separados por coma [ENTER omite]: " FOX_USERS
if [ -n "$FOX_USERS" ]; then
  read -r -p "Host de los usuarios FoxPro [%]: " FOX_HOST
  FOX_HOST="${FOX_HOST:-%}"
  IFS=',' read -r -a FOX_USER_LIST <<< "$FOX_USERS"
  for raw_user in "${FOX_USER_LIST[@]}"; do
    FOX_USER="$(echo "$raw_user" | xargs)"
    if [ -z "$FOX_USER" ]; then
      continue
    fi
    echo "== otorgar permisos FoxPro a '$FOX_USER'@'$FOX_HOST' =="
    run_sql_query "
GRANT SELECT, UPDATE ON \`$DB_NAME\`.facturas_ocr_cabecera TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT, UPDATE ON \`$DB_NAME\`.facturas_ocr_detalle TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT ON \`$DB_NAME\`.facturas_ocr_percepciones_iibb TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT, INSERT ON \`$DB_NAME\`.facturas_ocr_eventos TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT ON \`$DB_NAME\`.facturas_ocr_email_origen TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_pendientes TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_detalle TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_facturas_ocr_percepciones_iibb TO '$FOX_USER'@'$FOX_HOST';
GRANT SELECT, INSERT, UPDATE ON \`$DB_NAME\`.factura_reglas_contables TO '$FOX_USER'@'$FOX_HOST';
"
  done
  run_sql_query "FLUSH PRIVILEGES;"
fi

echo "OK: tablas staging facturas OCR listas"
