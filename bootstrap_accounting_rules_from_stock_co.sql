SET NAMES utf8mb4;

-- Bootstrap conservador de reglas contables desde el historico de stock_co.
-- Variables esperadas:
--   @min_score: porcentaje minimo de dominancia de una cuenta por proveedor.
--   @min_lineas: cantidad minima de lineas historicas del proveedor.

INSERT INTO factura_reglas_contables (
    proveedor_codigo,
    proveedor_nombre,
    patron_texto,
    patron_normalizado,
    palabras_clave,
    importe_min,
    importe_max,
    tolerancia_pct,
    periodicidad,
    cuenta_contable,
    cuenta_descripcion,
    prioridad,
    confianza,
    veces_usada,
    veces_confirmada,
    origen,
    requiere_revision,
    notas
)
SELECT
    CAST(x.proveedor AS CHAR) AS proveedor_codigo,
    LEFT(TRIM(COALESCE(p.NOMBRE, p.DENO, '')), 200) AS proveedor_nombre,
    '' AS patron_texto,
    '' AS patron_normalizado,
    '' AS palabras_clave,
    0.00 AS importe_min,
    0.00 AS importe_max,
    0.00 AS tolerancia_pct,
    'sin_definir' AS periodicidad,
    x.clave AS cuenta_contable,
    LEFT(TRIM(COALESCE(c.DESC0, '')), 100) AS cuenta_descripcion,
    50 AS prioridad,
    x.score AS confianza,
    x.lineas AS veces_usada,
    0 AS veces_confirmada,
    'historico_stock_co' AS origen,
    CASE WHEN x.score >= 95 THEN 0 ELSE 1 END AS requiere_revision,
    CONCAT(
        'Bootstrap desde stock_co: ',
        x.lineas,
        '/',
        x.total_lineas,
        ' lineas historicas en cuenta dominante.'
    ) AS notas
FROM (
    SELECT
        ranked.proveedor,
        ranked.clave,
        ranked.lineas,
        totals.total_lineas,
        ROUND((ranked.lineas / NULLIF(totals.total_lineas, 0)) * 100, 2) AS score
    FROM (
        SELECT
            PROVEEDOR AS proveedor,
            RTRIM(CLAVE) AS clave,
            COUNT(*) AS lineas,
            SUM(IMPORTE) AS importe_total,
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
) x
LEFT JOIN proveedo p ON p.PROVEEDOR = x.proveedor
LEFT JOIN contable c ON c.CODIGO = x.clave
WHERE x.score >= @min_score
  AND x.total_lineas >= @min_lineas
  AND x.clave <> ''
ON DUPLICATE KEY UPDATE
    proveedor_nombre = VALUES(proveedor_nombre),
    cuenta_descripcion = VALUES(cuenta_descripcion),
    confianza = GREATEST(confianza, VALUES(confianza)),
    veces_usada = GREATEST(veces_usada, VALUES(veces_usada)),
    origen = 'historico_stock_co',
    notas = VALUES(notas),
    updated_at = CURRENT_TIMESTAMP;

SELECT
    COUNT(*) AS reglas_historico_stock_co
FROM factura_reglas_contables
WHERE origen = 'historico_stock_co'
  AND activa = 1;
