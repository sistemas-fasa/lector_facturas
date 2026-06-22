SET @has_comprobante_codigo := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'facturas_ocr_cabecera'
    AND COLUMN_NAME = 'comprobante_codigo'
);

SET @add_comprobante_codigo := IF(
  @has_comprobante_codigo = 0,
  'ALTER TABLE facturas_ocr_cabecera ADD COLUMN comprobante_codigo VARCHAR(6) NULL AFTER comprobante_tipo',
  'SELECT 1'
);

PREPARE add_comprobante_codigo_stmt FROM @add_comprobante_codigo;
EXECUTE add_comprobante_codigo_stmt;
DEALLOCATE PREPARE add_comprobante_codigo_stmt;

UPDATE facturas_ocr_cabecera
SET comprobante_codigo = CASE
  WHEN comprobante_tipo = 'FACTURA' AND letra = 'A' THEN '001'
  WHEN comprobante_tipo = 'FACTURA' AND letra = 'B' THEN '006'
  WHEN comprobante_tipo = 'FACTURA' AND letra = 'C' THEN '011'
  WHEN comprobante_tipo = 'NOTA_DEBITO' AND letra = 'A' THEN '002'
  WHEN comprobante_tipo = 'NOTA_DEBITO' AND letra = 'B' THEN '007'
  WHEN comprobante_tipo = 'NOTA_DEBITO' AND letra = 'C' THEN '012'
  WHEN comprobante_tipo = 'NOTA_CREDITO' AND letra = 'A' THEN '003'
  WHEN comprobante_tipo = 'NOTA_CREDITO' AND letra = 'B' THEN '008'
  WHEN comprobante_tipo = 'NOTA_CREDITO' AND letra = 'C' THEN '013'
  ELSE comprobante_codigo
END
WHERE COALESCE(comprobante_codigo, '') = '';

CREATE OR REPLACE VIEW vw_facturas_ocr_pendientes AS
SELECT
  c.id,
  c.fecha_proceso,
  c.proveedor_codigo,
  c.proveedor_nombre,
  c.emisor_razon_social,
  c.emisor_cuit,
  c.comprobante_codigo,
  c.letra,
  c.punto_venta,
  c.numero,
  CONCAT(COALESCE(c.letra, ''), ' ', COALESCE(c.punto_venta, ''), '-', COALESCE(c.numero, '')) AS comprobante,
  c.fecha_emision,
  c.cae,
  c.cae_vencimiento,
  c.moneda,
  c.neto_gravado,
  c.iva_21,
  c.iva_105,
  c.iva_27,
  c.percepciones,
  c.percepciones_iibb,
  c.otros_impuestos,
  c.total,
  c.cuenta_contable_sugerida,
  c.cuenta_descripcion_sugerida,
  c.origen_sugerencia,
  c.score_sugerencia,
  c.requiere_confirmacion_contable,
  c.requiere_revision,
  c.observaciones,
  c.qr_detectado,
  c.archivo_original,
  c.json_file,
  eo.email_from,
  eo.email_subject,
  eo.email_date,
  eo.message_id
FROM facturas_ocr_cabecera c
LEFT JOIN facturas_ocr_email_origen eo ON eo.factura_id = c.id
WHERE c.importada = 0;

CREATE OR REPLACE VIEW vw_facturas_ocr_detalle AS
SELECT
  d.id,
  d.factura_id,
  d.linea,
  CAST(d.descripcion_factura AS CHAR(250)) AS descripcion_factura,
  d.cantidad,
  d.precio_unitario,
  d.subtotal,
  d.cuenta_contable,
  d.cuenta_descripcion,
  d.origen_sugerencia,
  d.score_sugerencia,
  d.requiere_confirmacion,
  d.confirmada,
  c.proveedor_codigo,
  c.proveedor_nombre,
  c.emisor_cuit,
  c.comprobante_codigo,
  c.letra,
  c.punto_venta,
  c.numero,
  c.fecha_emision
FROM facturas_ocr_detalle d
JOIN facturas_ocr_cabecera c ON c.id = d.factura_id;

CREATE OR REPLACE VIEW vw_facturas_ocr_percepciones_iibb AS
SELECT
  p.id,
  p.factura_id,
  c.emisor_razon_social,
  c.emisor_cuit,
  c.comprobante_codigo,
  c.letra,
  c.punto_venta,
  c.numero,
  c.fecha_emision,
  p.linea,
  p.jurisdiccion,
  p.codjur,
  p.importe
FROM facturas_ocr_percepciones_iibb p
JOIN facturas_ocr_cabecera c ON c.id = p.factura_id;
