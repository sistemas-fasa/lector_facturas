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
secundario, pero **no debe activarse en producción si n8n ya está leyendo la
misma casilla por IMAP**. Tener dos conexiones IMAP simultáneas a un mismo
buzón desde n8n (trigger IDLE) y desde invoice-parser (polling cada 60s)
provoca conflictos de conexión (ECONNRESET) con proveedores de correo como
Ferozo debido a límites de conexiones concurrentes o timeouts idle.

Ver `docs/n8n-email-ingestion.md` para el contrato completo.

## Despliegue FASA

Ver `deploy/fasa/README.md` para la configuracion productiva del servicio
`invoice-parser` en el servidor FASA, incluyendo plantillas de environment y
docker-compose que documentan por que `INVOICE_EMAIL_IMAP_POLL_ENABLED=false`
es obligatorio en produccion con n8n.

## Perfiles por Proveedor

Ver `docs/provider-profiles.md`.

Los perfiles permiten mejorar la extracción de campos por proveedor (total,
neto, IVA, percepciones, CAE, etc.) sin modificar las reglas globales de
parsing. Se configuran via `INVOICE_PROVIDER_PROFILES_FILE` y actúan como
fallback controlado sin pisar datos confiables del QR AFIP.

## API interna de consulta (visor de facturas)

El servicio expone endpoints GET para revisar facturas procesadas y estado
de cola sin exponer OCR completo. Ver `docs/internal-query-api.md`.

```
GET /invoices              — listado con filtros y resumen
GET /invoices/{id}         — detalle (OCR omitido)
GET /invoices/by-sha/{sha} — búsqueda por SHA256
GET /invoices/review       — atajo para requiere_revisión
GET /queue/status          — estado de cola
GET /queue/jobs            — listado de jobs
GET /admin/invoices        — panel HTML
```

## Seguridad

No versionar `.env`, adjuntos reales, reportes OCR, JSON/XML generados ni colas
de procesamiento. Esos archivos quedan excluidos por `.gitignore`.
