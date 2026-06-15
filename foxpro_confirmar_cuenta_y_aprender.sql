-- Plantilla para Visual FoxPro: confirmar cuenta de un detalle OCR y aprender la regla.
-- Reemplazar los placeholders ?detalle_id, ?cuenta_contable, ?cuenta_descripcion,
-- ?usuario, ?patron_texto y ?patron_normalizado desde SQLEXEC parametrizado.

UPDATE facturas_ocr_detalle
SET cuenta_contable = ?cuenta_contable,
    cuenta_descripcion = ?cuenta_descripcion,
    requiere_confirmacion = 0,
    confirmada = 1,
    usuario_confirmacion = ?usuario,
    fecha_confirmacion = NOW()
WHERE id = ?detalle_id;

INSERT INTO facturas_ocr_eventos (
  factura_id, detalle_id, sha256, evento, detalle, usuario
)
SELECT
  d.factura_id,
  d.id,
  d.sha256,
  'cuenta_confirmada',
  CONCAT('Cuenta confirmada: ', d.cuenta_contable, ' ', d.cuenta_descripcion),
  ?usuario
FROM facturas_ocr_detalle d
WHERE d.id = ?detalle_id;

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
  activa,
  primera_fecha,
  ultima_fecha,
  ultima_confirmacion,
  notas,
  created_at,
  updated_at
)
SELECT
  c.proveedor_codigo,
  c.proveedor_nombre,
  ?patron_texto,
  ?patron_normalizado,
  '',
  0,
  0,
  0,
  'sin_definir',
  d.cuenta_contable,
  d.cuenta_descripcion,
  100,
  100,
  1,
  1,
  'manual',
  0,
  1,
  c.fecha_emision,
  c.fecha_emision,
  NOW(),
  'Confirmada desde Visual FoxPro OCR',
  NOW(),
  NOW()
FROM facturas_ocr_detalle d
JOIN facturas_ocr_cabecera c ON c.id = d.factura_id
WHERE d.id = ?detalle_id
ON DUPLICATE KEY UPDATE
  cuenta_contable = VALUES(cuenta_contable),
  cuenta_descripcion = VALUES(cuenta_descripcion),
  confianza = 100,
  requiere_revision = 0,
  activa = 1,
  origen = 'manual',
  veces_confirmada = veces_confirmada + 1,
  ultima_confirmacion = NOW(),
  updated_at = NOW();
