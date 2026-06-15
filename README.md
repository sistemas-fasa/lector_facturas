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

## Seguridad

No versionar `.env`, adjuntos reales, reportes OCR, JSON/XML generados ni colas
de procesamiento. Esos archivos quedan excluidos por `.gitignore`.
