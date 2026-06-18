# Contrato de ingesta de email con n8n

Este documento fija el flujo productivo real para facturas recibidas por
correo. El lector y orquestador principal de correo es n8n. El servicio
Python `invoice-parser` recibe adjuntos por HTTP, los encola y ejecuta el
pipeline deterministico de extraccion y persistencia.

## Flujo productivo actual

```text
correo proveedor
  -> workflow n8n
  -> descarga/lee adjunto
  -> POST multipart a http://invoice-parser:8765/enqueue?source_type=email
  -> invoice-parser encola
  -> worker procesa
  -> QR AFIP / PDF text / OCR / reglas
  -> JSON/XML/.ready
  -> staging MySQL
  -> consumo Visual FoxPro
```

n8n solo entrega el archivo y la metadata disponible del correo. El
`invoice-parser` aplica el pipeline deterministico documentado en
`docs/architecture.md`. Cualquier fallback futuro de IA debe ser opcional,
auditable y posterior al contrato HTTP; no debe cambiar como n8n entrega
adjuntos al servicio.

## Contrato HTTP con n8n

Endpoint:

```text
POST /enqueue?source_type=email
Host interno Docker: invoice-parser:8765
URL completa desde n8n: http://invoice-parser:8765/enqueue?source_type=email
Content-Type: multipart/form-data
Campo obligatorio: file
```

El campo multipart `file` contiene el adjunto PDF/JPG/PNG que n8n descargo
del correo. El servicio rechaza el request si falta ese campo.

Respuesta basica esperada para un archivo nuevo:

```json
{
  "status": "QUEUED",
  "job_id": "...",
  "sha256": "...",
  "source_type": "email",
  "original_filename": "...",
  "queue_size": 1
}
```

Si se detecta un duplicado, la respuesta debe mantenerse como JSON simple y
estable:

```json
{
  "status": "DUPLICATE",
  "duplicate": true,
  "sha256": "...",
  "queue_size": 0,
  "original_filename": "..."
}
```

## Metadata soportada

Campos de metadata ya soportados por `invoice-parser` para el flujo n8n:

- `email_from`
- `email_to`
- `email_subject`
- `email_date`
- `email_message_id`
- `email_attachment_name`

Campos opcionales ya aceptados por el servicio y recomendados para futuras
mejoras del workflow n8n:

- `email_uid`
- `email_folder`
- `email_account`
- `email_attachment_index`
- `n8n_execution_id`
- `n8n_workflow_id`
- `n8n_workflow_name`
- `n8n_node_name`

Todos los campos de metadata son opcionales. Agregar metadata nueva no debe
romper requests existentes que solo envian `file` y la metadata basica.

## Responsabilidades

### n8n

- Leer correo desde la credencial IMAP configurada en n8n.
- Filtrar o seleccionar mensajes segun la politica del workflow.
- Descargar adjuntos y descartar formatos no soportados.
- Enviar cada adjunto a `POST /enqueue?source_type=email` como multipart
  usando el campo obligatorio `file`.
- Enviar metadata del correo cuando este disponible.
- Decidir cuando marcar el correo como leido, movido o archivado, salvo que
  se defina explicitamente otra politica.

### invoice-parser

- Recibir el adjunto por HTTP.
- Validar el request y el campo multipart `file`.
- Calcular `sha256`.
- Encolar el trabajo y responder rapido a n8n.
- Procesar la factura en segundo plano.
- Extraer QR AFIP, texto PDF, OCR y reglas locales.
- Persistir JSON/XML/.ready.
- Persistir staging MySQL para consumo desde Visual FoxPro.
- Reportar a n8n un estado simple y estable (`QUEUED`, `DUPLICATE` o
  `ERROR`).

## Reglas de compatibilidad

- No romper `POST /enqueue?source_type=email`.
- No cambiar el campo multipart obligatorio `file`.
- No cambiar la respuesta basica `QUEUED` sin mantener backward
  compatibility.
- Si se agrega o ajusta deduplicacion, mantener la respuesta `DUPLICATE`
  como JSON simple y estable.
- Si se agregan nuevos campos de metadata, deben ser opcionales.
- No hacer que n8n dependa de IA generativa.
- No mover al servicio Python la responsabilidad de marcar correos como
  leidos si el workflow principal sigue siendo n8n, salvo decision explicita.
- No cambiar workflows de n8n ni credenciales reales desde mejoras del
  parser sin una tarea especifica para eso.

## Relacion con la arquitectura hibrida

La arquitectura hibrida separa el transporte del archivo de la extraccion de
datos:

- n8n entrega el adjunto y metadata de correo.
- `invoice-parser` ejecuta el pipeline deterministico.
- La IA futura, si se habilita, solo puede actuar como fallback opcional y
  auditable.
- La IA no debe cambiar el contrato HTTP entre n8n e `invoice-parser`.

El contrato de ingesta queda antes del pipeline. Por eso una mejora en QR,
PDF text, OCR, reglas, staging o IA no debe requerir cambios en el nodo HTTP
de n8n mientras el adjunto siga llegando como `file`.

## Polling IMAP interno

`INVOICE_EMAIL_IMAP_POLL_ENABLED` existe como modo opcional y secundario del
sidecar Python. No es el flujo productivo principal cuando n8n esta activo.

Si se mejora el IMAP interno:

- Reutilizar helpers comunes de encolado, metadata y deduplicacion.
- No romper `/enqueue?source_type=email`.
- No asumir que Python marca correos como leidos en el flujo principal.
- Mantener la compatibilidad con el workflow n8n y el campo multipart `file`.

En el flujo productivo n8n, la politica de lectura/movido/archivo de correos
pertenece al workflow n8n.
