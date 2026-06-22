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

Modelo recomendado para validar facturas reales:

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemini-2.5-flash
OPENROUTER_FALLBACK_MODEL=
INVOICE_AI_ALLOW_FREE_MODELS=false
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

`google/gemini-2.5-flash-lite` queda como opcion economica para pruebas
controladas. `openrouter/free` solo debe usarse para probar conectividad, API
key y consumo contra OpenRouter. No se recomienda para facturas reales porque el
router puede elegir modelos que omiten campos fiscales criticos como
`punto_venta`, `codigo_afip`, letra o confianza.

Produccion debe usar modelos sin `:free` salvo decision explicita:

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemini-2.5-flash
OPENROUTER_FALLBACK_MODEL=google/gemini-2.5-flash-lite
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

Criterio de cierre del PR IA-first: el test live debe confirmar que Gemini
Flash levanta `punto_venta`, `codigo_afip`, numero, fecha, CUIT y total en una
factura real. Si no cumple esos campos, el siguiente paso es renderizar el PDF
como imagen antes de seguir ajustando prompts.

### Validacion live con OpenRouter pago

El test live usa la factura local
`muestras_privadas/02.05.2026 - tte hd FACTURA N°23796.pdf` y valida campos
criticos reales. Requiere `OPENROUTER_API_KEY` configurada en `.env` o en el
entorno y creditos disponibles en OpenRouter.

PowerShell:

```powershell
$env:RUN_OPENROUTER_LIVE_TESTS='1'
$env:OPENROUTER_MODEL='google/gemini-2.5-flash'
$env:OPENROUTER_FALLBACK_MODEL=''
$env:INVOICE_AI_ALLOW_FREE_MODELS='false'
python -m pytest test_openrouter_live_invoice.py -q
```

Para probar la opcion economica, cambiar solo:

```powershell
$env:OPENROUTER_MODEL='google/gemini-2.5-flash-lite'
```

El PR no debe salir de draft hasta que el test live pase con un modelo pago
fijo. Si falla por campos fiscales faltantes, el siguiente paso es renderizar el
PDF como imagen antes de seguir tocando prompts.

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
