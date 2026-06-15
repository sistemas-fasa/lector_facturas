CREATE TABLE IF NOT EXISTS facturas_ocr_percepciones_iibb (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  factura_id BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL,
  linea INT NOT NULL,
  jurisdiccion VARCHAR(80) NULL,
  codjur VARCHAR(20) NULL,
  importe DECIMAL(15,2) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_facturas_ocr_iibb_linea (factura_id, linea),
  KEY ix_facturas_ocr_iibb_sha (sha256),
  KEY ix_facturas_ocr_iibb_codjur (codjur),
  CONSTRAINT fk_facturas_ocr_iibb_cabecera
    FOREIGN KEY (factura_id) REFERENCES facturas_ocr_cabecera(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE OR REPLACE VIEW vw_facturas_ocr_percepciones_iibb AS
SELECT
  p.id,
  p.factura_id,
  c.emisor_razon_social,
  c.emisor_cuit,
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

-- Opcional: ejecutar con un usuario administrador si queres dejar permisos listos.
-- Ajustar usuario/host si en tu instalacion tienen otros nombres.
GRANT SELECT, INSERT, UPDATE, DELETE ON fasa.facturas_ocr_percepciones_iibb TO 'invoice_parser_ro'@'%';
GRANT SELECT ON fasa.vw_facturas_ocr_percepciones_iibb TO 'invoice_parser_ro'@'%';

-- Correccion puntual del registro detectado con OCR erroneo.
-- El monto 9042807692.00 no es una percepcion valida y queda para revision.
UPDATE facturas_ocr_cabecera
SET percepciones = 0,
    percepciones_iibb = 0,
    requiere_revision = 1,
    observaciones = TRIM(BOTH '; ' FROM CONCAT(COALESCE(observaciones, ''), '; Percepcion IIBB descartada por monto OCR invalido'))
WHERE id = 7
  AND percepciones_iibb = 9042807692.00;
