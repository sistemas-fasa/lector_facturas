# Lector de Facturas

Parser de facturas para n8n, MySQL staging y consumo desde Visual FoxPro.

El flujo principal nuevo es **AI-first via OpenRouter**: el sidecar intenta
extraer la factura desde el archivo original con IA multimodal, valida la
respuesta localmente con reglas duras y solo despues cae al pipeline legacy
de PDF text / QR / OCR / PyOCR / regex.

## Componentes principales

- `host_invoice_parser_service.py`: sidecar HTTP para recibir archivos desde n8n.
- `invoice_ai_extractor/`: extraccion AI-first via OpenRouter, validacion local
  y orquestacion de fallback legacy.
- `invoice_parser_helpers.py`: armado de JSON legacy, JSON enriquecido, XML y staging.
- `factura_ocr/`: reglas locales de extracción sobre texto PDF/OCR.
- `facturas_ocr_staging.sql`: tablas y vistas MySQL para FoxPro.
- `vfp_facturas_ocr_staging.prg`: helpers Visual FoxPro para consultar staging.

## Validación local

```powershell
python -m pytest -q
python -m compileall invoice_parser_helpers.py host_invoice_parser_service.py invoice_parser_runtime.py factura_ocr
```

## Arquitectura AI-first

Ver `docs/architecture.md` para el detalle de la arquitectura:

- **Pipeline principal**: archivo original → OpenRouter AI extractor →
  validacion local dura → staging MySQL.
- **Fallback legacy**: QR AFIP → PDF text → OCR Tesseract → reglas locales →
  validacion cruzada, usado solo si IA esta deshabilitada, falla, devuelve JSON
  invalido o no supera validaciones.

## Configuracion IA

Desarrollo/staging puede usar modelos free explicitamente:

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
OPENROUTER_FALLBACK_MODEL=
INVOICE_AI_ALLOW_FREE_MODELS=true
INVOICE_AI_TIMEOUT_SECONDS=60
INVOICE_AI_MAX_RETRIES=1
INVOICE_AI_DEBUG=true
INVOICE_AI_STORE_RAW_RESPONSE=true
INVOICE_AI_LOG_RAW_RESPONSE=false
INVOICE_AI_MIN_CONFIDENCE=0.70
INVOICE_AI_REQUIRE_CRITICAL_FIELDS=true
INVOICE_AI_TOTAL_TOLERANCE=2.00
INVOICE_AI_FALLBACK_LEGACY=true
```

Produccion debe usar modelos sin `:free` salvo decision explicita:

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemini-2.5-flash-lite
OPENROUTER_FALLBACK_MODEL=google/gemini-2.5-flash
INVOICE_AI_ALLOW_FREE_MODELS=false
INVOICE_AI_TIMEOUT_SECONDS=45
INVOICE_AI_MAX_RETRIES=2
INVOICE_AI_DEBUG=false
INVOICE_AI_STORE_RAW_RESPONSE=true
INVOICE_AI_LOG_RAW_RESPONSE=false
INVOICE_AI_MIN_CONFIDENCE=0.75
INVOICE_AI_REQUIRE_CRITICAL_FIELDS=true
INVOICE_AI_TOTAL_TOLERANCE=2.00
INVOICE_AI_FALLBACK_LEGACY=true
```

Si `OPENROUTER_MODEL` es `openrouter/free` o contiene `:free` y
`INVOICE_AI_ALLOW_FREE_MODELS` no es `true`, la IA no se usa y el sistema cae
al fallback legacy con error controlado en trazabilidad.

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
