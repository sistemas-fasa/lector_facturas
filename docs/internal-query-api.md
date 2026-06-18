# Internal Query API — Visor de Facturas

Endpoints de consulta (GET) incorporados en `host_invoice_parser_service.py`
para revisar facturas procesadas sin exponer OCR completo.

## Objetivo

Proveer un MVP interno para revisar el estado de facturas parseadas,
inspeccionar métricas de calidad y depurar errores de extracción sin
necesidad de acceso directo al filesystem o MySQL.

## Endpoints

### `GET /health`

Estado del servicio. No requiere autenticación.

### `GET /invoices`

Listado paginado de facturas con filtros.

| Param | Tipo | Default | Descripción |
|---|---|---|---|
| `limit` | int | 50 | Máx 200 |
| `offset` | int | 0 | |
| `status` | str | — | `OK`, `REVIEW_REQUIRED`, `ERROR`, `DUPLICATE` |
| `requires_review` | bool | — | `true` filtra las que requieren revisión |
| `provider_cuit` | str | — | Búsqueda parcial |
| `provider_name` | str | — | Búsqueda parcial (case-insensitive) |
| `date_from` | str | — | `YYYY-MM-DD` (fecha emisión) |
| `date_to` | str | — | `YYYY-MM-DD` |
| `document_type` | str | — | `FACTURA`, `ILEGIBLE`, `NO_FISCAL`, etc. |
| `invoice_type` | str | — | `FACTURA`, `NOTA_CREDITO`, etc. |
| `q` | str | — | Búsqueda textual en varios campos |

Respuesta incluye `items`, `total`, `summary` (ok, review_required, errors, duplicates, non_invoice, por_proveedor, fallas_frecuentes).

### `GET /invoices/review`

Atajo para `requires_review=true`. Misma respuesta que `/invoices`.

### `GET /invoices/{id}`

Detalle completo de una factura por ID numérico (1-indexado según orden
alfabético descendente de archivos JSON, o por ID de staging si MySQL está
configurado). El OCR text **no se incluye** en la respuesta
(`ocr_text_omitted: true`).

### `GET /invoices/by-sha/{sha256}`

Búsqueda por SHA256 completo o prefijo (mín 6 caracteres hex).

### `GET /queue/status`

Estado de la cola de procesamiento: pendientes, procesados, errores,
flags de configuración.

### `GET /queue/jobs`

Listado de jobs en cola. Filtros: `limit`, `offset`, `status`
(`pendientes`, `procesados`, `errores`).

### `GET /queue/jobs/{job_id}`

Detalle de un job. Campos sensibles (password, token, api_key)
redactados con `**REDACTED**`.

### `GET /admin/invoices`

Panel HTML con tabla de facturas, resumen superior y filtros inline.
Requiere `INVOICE_ADMIN_TOKEN` si está configurado.

### `GET /admin/invoices/{id}`

Detalle HTML de una factura con tarjetas por sección (datos principales,
importes, diagnóstico, extracción, email/origen, archivos).

## Seguridad

### `INVOICE_ADMIN_TOKEN`

Variable de entorno opcional. Cuando está vacía (default), todos los
endpoints GET (excepto `/health`) son accesibles sin autenticación.

Cuando se define un valor, los endpoints requieren:

```
Authorization: Bearer <token>
```

O como query param:

```
GET /invoices?token=<token>
```

El endpoint `/health` nunca requiere token.

### Advertencia de datos fiscales

Los endpoints exponen datos de facturas (CUIT, montos, razones sociales).
El acceso debe limitarse a la red interna. Se recomienda usar SSH tunnel
o iptables para restringir origen.

## Consideraciones

- **OCR no se expone**: los detalles JSON incluyen `ocr_text_omitted: true`
  y sólo reportan cantidad de caracteres (`ocr_chars`). El texto OCR completo
  no es accesible por API.
- **Issue #22 pendiente**: no hay endpoints seguros para visualizar/descargar
  archivos originales (PDF) ni evidencia OCR.
- **Read-only**: ningún endpoint GET modifica archivos, la cola o MySQL.
- **Compatibilidad**: 100% backward compatible — no se modifican rutas POST
  (`/enqueue`, `/parse`) ni el formato multipart `file`.

## Ejemplos curl

```bash
# Listar facturas que requieren revisión
curl http://invoice-parser:8765/invoices/review

# Listar con filtros
curl "http://invoice-parser:8765/invoices?status=OK&provider_name=baw&limit=5"

# Detalle
curl http://invoice-parser:8765/invoices/1

# Buscar por SHA
curl http://invoice-parser:8765/invoices/by-sha/abcdef1234567890

# Panel HTML
curl http://invoice-parser:8765/admin/invoices

# Con token
curl -H "Authorization: Bearer mi-token-secreto" http://invoice-parser:8765/invoices
curl "http://invoice-parser:8765/invoices?token=mi-token-secreto"
```

## SSH tunnel

```bash
ssh -L 8765:localhost:8765 usuario@servidor-fasa
# Luego: http://localhost:8765/invoices
```
