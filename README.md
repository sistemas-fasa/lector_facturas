# Lector de Facturas

Parser local de facturas para n8n, MySQL staging y consumo desde Visual FoxPro.

El flujo evita IA generativa: usa lectura de texto PDF, OCR local con Tesseract,
QR AFIP, reglas Python y persistencia MySQL.

## Componentes principales

- `host_invoice_parser_service.py`: sidecar HTTP para recibir archivos desde n8n.
- `invoice_parser_helpers.py`: armado de JSON legacy, JSON enriquecido, XML y staging.
- `factura_ocr/`: reglas locales de extracción sobre texto PDF/OCR.
- `facturas_ocr_staging.sql`: tablas y vistas MySQL para FoxPro.
- `vfp_facturas_ocr_staging.prg`: helpers Visual FoxPro para consultar staging.

## Validación local

```powershell
python -m pytest -q
python -m compileall invoice_parser_helpers.py host_invoice_parser_service.py invoice_parser_runtime.py factura_ocr
```

## Arquitectura híbrida

Ver `docs/architecture.md` para el detalle de la arquitectura:

- **Pipeline determinístico** (siempre activo): QR AFIP → PDF text → OCR Tesseract
  → reglas locales → validación cruzada → staging MySQL.
- **Fallback IA** (desactivado por defecto): opcional, solo para casos
  `REVIEW_REQUIRED`, nunca reemplaza datos del QR AFIP.

## Ingesta de correo con n8n

El flujo productivo recomendado para correo es n8n como lector/orquestador y
`invoice-parser` como receptor asincronico:

```text
n8n -> POST http://invoice-parser:8765/enqueue?source_type=email
```

El request debe ser `multipart/form-data` con el adjunto en el campo `file`.
El polling IMAP interno del servicio Python existe solo como modo opcional o
secundario. Ver `docs/n8n-email-ingestion.md` para el contrato completo.

## Seguridad

No versionar `.env`, adjuntos reales, reportes OCR, JSON/XML generados ni colas
de procesamiento. Esos archivos quedan excluidos por `.gitignore`.
