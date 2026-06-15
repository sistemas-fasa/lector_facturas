-- Plantilla para Visual FoxPro: marcar una factura OCR como importada.
-- Reemplazar ?factura_id, ?usuario y ?observaciones desde SQLEXEC parametrizado.

UPDATE facturas_ocr_cabecera
SET importada = 1,
    fecha_importacion = NOW(),
    usuario_importacion = ?usuario,
    observaciones_importacion = ?observaciones
WHERE id = ?factura_id;

INSERT INTO facturas_ocr_eventos (
  factura_id, sha256, evento, detalle, usuario
)
SELECT
  id,
  sha256,
  'factura_importada',
  'Factura importada en Visual FoxPro',
  ?usuario
FROM facturas_ocr_cabecera
WHERE id = ?factura_id;
