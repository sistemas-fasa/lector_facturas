CREATE TABLE IF NOT EXISTS facturas_ocr_cabecera (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sha256 CHAR(64) NOT NULL,
  file_hash CHAR(64) NULL,
  source_type VARCHAR(50) NULL,
  archivo_original VARCHAR(255) NULL,
  archivo_nombre VARCHAR(255) NULL,
  json_file VARCHAR(500) NULL,
  xml_file VARCHAR(500) NULL,
  ready_file VARCHAR(500) NULL,
  estado VARCHAR(30) NOT NULL DEFAULT 'OK',
  status VARCHAR(30) NULL,
  error_message TEXT NULL,
  requiere_revision TINYINT(1) NOT NULL DEFAULT 0,
  observaciones TEXT NULL,
  fecha_proceso DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

  proveedor_codigo VARCHAR(30) NULL,
  proveedor_nombre VARCHAR(160) NULL,
  emisor_razon_social VARCHAR(160) NULL,
  emisor_cuit VARCHAR(20) NULL,
  emisor_iva_condicion VARCHAR(80) NULL,
  emisor_domicilio VARCHAR(255) NULL,

  receptor_razon_social VARCHAR(160) NULL,
  receptor_cuit VARCHAR(20) NULL,
  receptor_iva_condicion VARCHAR(80) NULL,

  comprobante_tipo VARCHAR(30) NOT NULL DEFAULT 'FACTURA',
  letra CHAR(1) NULL,
  punto_venta VARCHAR(8) NULL,
  numero VARCHAR(20) NULL,
  fecha_emision DATE NULL,
  fecha_vencimiento DATE NULL,
  cae VARCHAR(30) NULL,
  cae_vencimiento DATE NULL,
  moneda VARCHAR(5) NOT NULL DEFAULT 'ARS',

  neto_gravado DECIMAL(15,2) NOT NULL DEFAULT 0,
  iva_21 DECIMAL(15,2) NOT NULL DEFAULT 0,
  iva_105 DECIMAL(15,2) NOT NULL DEFAULT 0,
  iva_27 DECIMAL(15,2) NOT NULL DEFAULT 0,
  exento DECIMAL(15,2) NOT NULL DEFAULT 0,
  no_gravado DECIMAL(15,2) NOT NULL DEFAULT 0,
  percepciones DECIMAL(15,2) NOT NULL DEFAULT 0,
  percepciones_iibb DECIMAL(15,2) NOT NULL DEFAULT 0,
  otros_impuestos DECIMAL(15,2) NOT NULL DEFAULT 0,
  total DECIMAL(15,2) NOT NULL DEFAULT 0,

  cuenta_contable_sugerida VARCHAR(30) NULL,
  cuenta_descripcion_sugerida VARCHAR(160) NULL,
  origen_sugerencia VARCHAR(50) NULL,
  score_sugerencia DECIMAL(5,2) NOT NULL DEFAULT 0,
  requiere_confirmacion_contable TINYINT(1) NOT NULL DEFAULT 1,

  qr_detectado TINYINT(1) NOT NULL DEFAULT 0,
  ocr_motor VARCHAR(80) NULL,
  ocr_confianza DECIMAL(6,2) NULL,
  ocr_text LONGTEXT NULL,
  pdf_text LONGTEXT NULL,
  qr_raw LONGTEXT NULL,
  extracted_json_enriched JSON NULL,
  extracted_json_legacy JSON NULL,

  importada TINYINT(1) NOT NULL DEFAULT 0,
  fecha_importacion DATETIME NULL,
  usuario_importacion VARCHAR(80) NULL,
  observaciones_importacion TEXT NULL,

  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_facturas_ocr_cabecera_sha256 (sha256),
  KEY ix_facturas_ocr_pendientes (importada, requiere_revision, created_at),
  KEY ix_facturas_ocr_comprobante (emisor_cuit, letra, punto_venta, numero),
  KEY ix_facturas_ocr_proveedor (proveedor_codigo, emisor_cuit),
  KEY ix_facturas_ocr_fecha (fecha_emision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS facturas_ocr_detalle (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  factura_id BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL,
  linea INT NOT NULL,

  descripcion_factura VARCHAR(500) NULL,
  cantidad DECIMAL(15,4) NULL,
  precio_unitario DECIMAL(15,4) NULL,
  subtotal DECIMAL(15,2) NULL,

  cuenta_contable VARCHAR(30) NULL,
  cuenta_descripcion VARCHAR(160) NULL,
  origen_sugerencia VARCHAR(50) NULL,
  score_sugerencia DECIMAL(5,2) NOT NULL DEFAULT 0,
  requiere_confirmacion TINYINT(1) NOT NULL DEFAULT 1,
  confirmada TINYINT(1) NOT NULL DEFAULT 0,
  usuario_confirmacion VARCHAR(80) NULL,
  fecha_confirmacion DATETIME NULL,

  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_facturas_ocr_detalle_linea (factura_id, linea),
  KEY ix_facturas_ocr_detalle_sha (sha256),
  KEY ix_facturas_ocr_detalle_cuenta (cuenta_contable),
  CONSTRAINT fk_facturas_ocr_detalle_cabecera
    FOREIGN KEY (factura_id) REFERENCES facturas_ocr_cabecera(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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

CREATE TABLE IF NOT EXISTS facturas_ocr_eventos (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  factura_id BIGINT UNSIGNED NULL,
  detalle_id BIGINT UNSIGNED NULL,
  sha256 CHAR(64) NULL,
  evento VARCHAR(80) NOT NULL,
  detalle TEXT NULL,
  usuario VARCHAR(80) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  KEY ix_facturas_ocr_eventos_factura (factura_id, created_at),
  KEY ix_facturas_ocr_eventos_sha (sha256),
  CONSTRAINT fk_facturas_ocr_eventos_cabecera
    FOREIGN KEY (factura_id) REFERENCES facturas_ocr_cabecera(id)
    ON DELETE SET NULL,
  CONSTRAINT fk_facturas_ocr_eventos_detalle
    FOREIGN KEY (detalle_id) REFERENCES facturas_ocr_detalle(id)
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS facturas_ocr_email_origen (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  factura_id BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL,
  message_id VARCHAR(255) NULL,
  email_from VARCHAR(255) NULL,
  email_to VARCHAR(255) NULL,
  email_subject VARCHAR(500) NULL,
  email_date VARCHAR(120) NULL,
  attachment_name VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_facturas_ocr_email_sha_attachment (sha256, attachment_name),
  KEY ix_facturas_ocr_email_factura (factura_id),
  KEY ix_facturas_ocr_email_message (message_id),
  CONSTRAINT fk_facturas_ocr_email_cabecera
    FOREIGN KEY (factura_id) REFERENCES facturas_ocr_cabecera(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE OR REPLACE VIEW vw_facturas_ocr_pendientes AS
SELECT
  c.id,
  c.fecha_proceso,
  c.proveedor_codigo,
  c.proveedor_nombre,
  c.emisor_razon_social,
  c.emisor_cuit,
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
  d.descripcion_factura,
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
