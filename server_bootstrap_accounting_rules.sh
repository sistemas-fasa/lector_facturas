#!/usr/bin/env bash
set -euo pipefail

SQL_FILE="${SQL_FILE:-/tmp/bootstrap_accounting_rules_from_stock_co.sql}"
DB_NAME_DEFAULT="${FASA_MYSQL_DATABASE:-fasa}"

echo "== bootstrap reglas contables desde stock_co =="

if ! command -v mysql >/dev/null 2>&1; then
  echo "ERROR: no se encontro el cliente mysql en el servidor." >&2
  exit 1
fi

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
read -r -p "Usuario MySQL con permisos INSERT/UPDATE sobre reglas [$USER]: " DB_USER
DB_USER="${DB_USER:-$USER}"
read -r -s -p "Password MySQL para $DB_USER: " DB_PASS
echo
read -r -p "Score minimo para crear regla [95]: " MIN_SCORE
MIN_SCORE="${MIN_SCORE:-95}"
read -r -p "Minimo de lineas historicas por proveedor [3]: " MIN_LINEAS
MIN_LINEAS="${MIN_LINEAS:-3}"

MYSQL=(mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER")
if [ -n "${DB_PASS:-}" ]; then
  MYSQL+=("-p$DB_PASS")
fi

echo "== verificar conexion y tablas =="
"${MYSQL[@]}" "$DB_NAME" -e "
SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('stock_co','proveedo','contable','factura_reglas_contables')
ORDER BY TABLE_NAME;
"

echo "== vista previa por rango de confianza =="
"${MYSQL[@]}" "$DB_NAME" -e "
SELECT bucket, COUNT(*) AS proveedores
FROM (
  SELECT
    proveedor,
    CASE
      WHEN score >= 95 THEN '>=95'
      WHEN score >= 90 THEN '90-94.99'
      WHEN score >= 85 THEN '85-89.99'
      WHEN score >= 75 THEN '75-84.99'
      ELSE '<75'
    END AS bucket
  FROM (
    SELECT
      ranked.proveedor,
      ROUND((ranked.lineas / NULLIF(totals.total_lineas, 0)) * 100, 2) AS score
    FROM (
      SELECT
        PROVEEDOR AS proveedor,
        RTRIM(CLAVE) AS clave,
        COUNT(*) AS lineas,
        ROW_NUMBER() OVER (
          PARTITION BY PROVEEDOR
          ORDER BY COUNT(*) DESC, SUM(IMPORTE) DESC, RTRIM(CLAVE)
        ) AS rn
      FROM stock_co
      WHERE RTRIM(COALESCE(CLAVE, '')) <> ''
      GROUP BY PROVEEDOR, RTRIM(CLAVE)
    ) ranked
    JOIN (
      SELECT PROVEEDOR AS proveedor, COUNT(*) AS total_lineas
      FROM stock_co
      WHERE RTRIM(COALESCE(CLAVE, '')) <> ''
      GROUP BY PROVEEDOR
    ) totals ON totals.proveedor = ranked.proveedor
    WHERE ranked.rn = 1
      AND totals.total_lineas >= $MIN_LINEAS
  ) scored
) buckets
GROUP BY bucket
ORDER BY CASE bucket
  WHEN '>=95' THEN 1
  WHEN '90-94.99' THEN 2
  WHEN '85-89.99' THEN 3
  WHEN '75-84.99' THEN 4
  ELSE 5
END;
"

echo "== reglas que se cargarian con score >= $MIN_SCORE y lineas >= $MIN_LINEAS =="
"${MYSQL[@]}" "$DB_NAME" -e "
SELECT COUNT(*) AS reglas_a_insertar_o_actualizar
FROM (
  SELECT ranked.proveedor
  FROM (
    SELECT
      PROVEEDOR AS proveedor,
      RTRIM(CLAVE) AS clave,
      COUNT(*) AS lineas,
      ROW_NUMBER() OVER (
        PARTITION BY PROVEEDOR
        ORDER BY COUNT(*) DESC, SUM(IMPORTE) DESC, RTRIM(CLAVE)
      ) AS rn
    FROM stock_co
    WHERE RTRIM(COALESCE(CLAVE, '')) <> ''
    GROUP BY PROVEEDOR, RTRIM(CLAVE)
  ) ranked
  JOIN (
    SELECT PROVEEDOR AS proveedor, COUNT(*) AS total_lineas
    FROM stock_co
    WHERE RTRIM(COALESCE(CLAVE, '')) <> ''
    GROUP BY PROVEEDOR
  ) totals ON totals.proveedor = ranked.proveedor
  WHERE ranked.rn = 1
    AND totals.total_lineas >= $MIN_LINEAS
    AND ROUND((ranked.lineas / NULLIF(totals.total_lineas, 0)) * 100, 2) >= $MIN_SCORE
) candidates;
"

read -r -p "Confirmas cargar/actualizar estas reglas? Escribi SI: " CONFIRM
if [ "$CONFIRM" != "SI" ]; then
  echo "Cancelado. No se modifico la base."
  exit 0
fi

tmp_sql="$(mktemp)"
{
  echo "SET @min_score := $MIN_SCORE;"
  echo "SET @min_lineas := $MIN_LINEAS;"
  cat "$SQL_FILE"
} > "$tmp_sql"

echo "== ejecutando bootstrap =="
"${MYSQL[@]}" "$DB_NAME" < "$tmp_sql"
rm -f "$tmp_sql"

echo "== muestra de reglas cargadas =="
"${MYSQL[@]}" "$DB_NAME" -e "
SELECT proveedor_codigo, proveedor_nombre, cuenta_contable, cuenta_descripcion, confianza, veces_usada, requiere_revision
FROM factura_reglas_contables
WHERE origen = 'historico_stock_co'
ORDER BY confianza DESC, veces_usada DESC
LIMIT 20;
"

echo "OK: bootstrap de reglas contables finalizado"
