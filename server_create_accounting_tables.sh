#!/usr/bin/env bash
set -euo pipefail

SQL_FILE="${SQL_FILE:-/tmp/contabilidad_aprendizaje.sql}"
DB_NAME_DEFAULT="${FASA_MYSQL_DATABASE:-fasa}"

echo "== crear tablas contables para facturas OCR =="

if ! command -v mysql >/dev/null 2>&1; then
  echo "ERROR: no se encontro el cliente mysql en el servidor." >&2
  echo "Instalalo o ejecuta el SQL manualmente desde un cliente con permisos DDL." >&2
  exit 1
fi

if [ ! -f "$SQL_FILE" ]; then
  echo "ERROR: no existe $SQL_FILE" >&2
  exit 1
fi

read -r -p "Host MySQL [localhost]: " DB_HOST
DB_HOST="${DB_HOST:-localhost}"
read -r -p "Puerto MySQL [3306]: " DB_PORT
DB_PORT="${DB_PORT:-3306}"
read -r -p "Base MySQL [$DB_NAME_DEFAULT]: " DB_NAME
DB_NAME="${DB_NAME:-$DB_NAME_DEFAULT}"
read -r -p "Usuario MySQL con permisos CREATE/ALTER [$USER]: " DB_USER
DB_USER="${DB_USER:-$USER}"
read -r -s -p "Password MySQL para $DB_USER: " DB_PASS
echo

if [ -z "$DB_NAME" ] || [ -z "$DB_USER" ]; then
  echo "ERROR: base y usuario son obligatorios" >&2
  exit 1
fi

MYSQL=(mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER")
if [ -n "${DB_PASS:-}" ]; then
  MYSQL+=("-p$DB_PASS")
fi

echo "== verificar conexion =="
"${MYSQL[@]}" -e "SELECT DATABASE();" "$DB_NAME" >/dev/null

echo "== ejecutar schema =="
"${MYSQL[@]}" "$DB_NAME" < "$SQL_FILE"

echo "== verificar tablas =="
"${MYSQL[@]}" "$DB_NAME" -e "
SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('factura_ejemplos_contables','factura_reglas_contables','vw_factura_reglas_contables_activas')
ORDER BY TABLE_NAME;
"

read -r -p "Usuario read-only del parser para otorgar SELECT [invoice_parser_ro, ENTER omite]: " RO_USER
RO_USER="${RO_USER:-invoice_parser_ro}"
if [ -n "$RO_USER" ]; then
  read -r -p "Host del usuario read-only [%]: " RO_HOST
  RO_HOST="${RO_HOST:-%}"
  echo "== otorgar SELECT a '$RO_USER'@'$RO_HOST' =="
  "${MYSQL[@]}" "$DB_NAME" -e "
GRANT SELECT ON \`$DB_NAME\`.proveedo TO '$RO_USER'@'$RO_HOST';
GRANT SELECT ON \`$DB_NAME\`.stock_co TO '$RO_USER'@'$RO_HOST';
GRANT SELECT ON \`$DB_NAME\`.contable TO '$RO_USER'@'$RO_HOST';
GRANT SELECT ON \`$DB_NAME\`.factura_reglas_contables TO '$RO_USER'@'$RO_HOST';
GRANT SELECT ON \`$DB_NAME\`.factura_ejemplos_contables TO '$RO_USER'@'$RO_HOST';
GRANT SELECT ON \`$DB_NAME\`.vw_factura_reglas_contables_activas TO '$RO_USER'@'$RO_HOST';
FLUSH PRIVILEGES;
"
fi

echo "OK: tablas contables listas"
