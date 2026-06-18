# Facturas por mail hacia OCR y FoxPro

## Objetivo

Compras reenvia facturas a una casilla IMAP. n8n toma los adjuntos PDF/JPG/PNG, los manda al sidecar `invoice-parser`, y el parser guarda cabecera/detalle en MySQL para que Visual FoxPro lea desde las vistas staging.

El flujo productivo principal es n8n -> `POST /enqueue?source_type=email`.
El polling IMAP interno del servicio Python es opcional/secundario. El
contrato estable entre n8n e `invoice-parser` esta documentado en
`docs/n8n-email-ingestion.md`.

## Variables necesarias

Copiar las claves de `.env.email.example` al `.env` real de `lector_factura`.

Minimo requerido para crear/actualizar el workflow n8n:

```env
N8N_API_URL=http://127.0.0.1:5678
N8N_API_KEY=...
INVOICE_EMAIL_HELPER_URL=http://invoice-parser:8765/enqueue
INVOICE_EMAIL_WORKFLOW_NAME=Facturas OCR - Email a staging FoxPro
INVOICE_EMAIL_FOLDER=INBOX
INVOICE_EMAIL_ALLOWED_EXTENSIONS=pdf,jpg,jpeg,png
INVOICE_EMAIL_ALLOWED_SENDERS=@fasa.ar,@ferreteriaavenida.com.ar
INVOICE_EMAIL_PARSE_TIMEOUT_MS=600000
INVOICE_QR_MAX_PAGES=2
INVOICE_QR_ZBAR_TIMEOUT_SECONDS=2
INVOICE_QR_MAX_VARIANTS=10
INVOICE_EMAIL_IMAP_CREDENTIAL_ID=
INVOICE_EMAIL_IMAP_CREDENTIAL_NAME=Facturas Compras IMAP
```

El password del correo no se guarda dentro del workflow JSON. Lo maneja n8n como credencial IMAP. Si se necesita probar o crear la credencial, tener a mano:

```env
INVOICE_EMAIL_IMAP_HOST=
INVOICE_EMAIL_IMAP_PORT=993
INVOICE_EMAIL_IMAP_USER=
INVOICE_EMAIL_IMAP_PASSWORD=
INVOICE_EMAIL_IMAP_SSL=true
```

Variables opcionales solo para el polling IMAP interno del sidecar Python:

```env
INVOICE_EMAIL_IMAP_POLL_ENABLED=false
INVOICE_EMAIL_IMAP_POLL_INTERVAL_SECONDS=60
```

No habilitar ese polling como reemplazo implicito del workflow n8n. Si se
activa, debe tratarse como modo secundario y reutilizar el mismo encolado.

## Orden de aplicacion

1. Crear o actualizar tablas staging:

```powershell
.\run_server_create_invoice_staging_tables.ps1
```

Esto agrega tambien `facturas_ocr_email_origen`, donde queda la trazabilidad del mail: remitente, asunto, fecha, message-id y nombre del adjunto.

2. Redeploy del sidecar:

```powershell
.\run_server_setup_n8n_invoice_parser.ps1
```

3. Crear o actualizar workflow de mail:

```powershell
.\run_create_n8n_invoice_email_workflow.ps1
```

El workflow queda inactivo. Revisar en n8n que el nodo `Leer Mail Facturas` tenga la credencial IMAP correcta y activarlo cuando este probado.

## Flujo generado en n8n

```text
Leer Mail Facturas
  -> Extraer Adjuntos
  -> Enviar a Parser OCR
  -> Registrar Resultado
```

`Extraer Adjuntos` filtra por remitente permitido, extension/mime type y envia cada adjunto como un item separado. `INVOICE_EMAIL_ALLOWED_SENDERS` acepta direcciones completas o dominios, separados por coma. Ejemplos: `compras@fasa.ar`, `@fasa.ar`, `fasa.ar`.

`Enviar a Parser OCR` manda multipart al endpoint asincronico del sidecar:

```text
http://invoice-parser:8765/enqueue?source_type=email
```

El sidecar responde rapido `QUEUED` y procesa el OCR en segundo plano. Esto evita que n8n quede esperando facturas pesadas o varios adjuntos. Luego el worker escribe cabecera/detalle en MySQL.

El multipart incluye:

- `file`
- `email_from`
- `email_to`
- `email_subject`
- `email_date`
- `email_message_id`
- `email_attachment_name`

El servicio tambien acepta metadata opcional para futuras mejoras:

- `email_uid`
- `email_folder`
- `email_account`
- `email_attachment_index`
- `n8n_execution_id`
- `n8n_workflow_id`
- `n8n_workflow_name`
- `n8n_node_name`

El parser persiste cabecera/detalle en:

- `facturas_ocr_cabecera`
- `facturas_ocr_detalle`
- `facturas_ocr_eventos`
- `facturas_ocr_email_origen`

Si `INVOICE_EMAIL_IMAP_POLL_ENABLED=true`, el sidecar tambien puede leer la
casilla IMAP directo, sin depender del trigger de n8n. Ese modo toma mails no
leidos, encola los adjuntos permitidos y marca el mail como leido despues de
encolar. No es el flujo productivo principal si n8n esta activo, y no debe
usarse para mover la responsabilidad de marcado de correos al servicio Python
sin una decision explicita.

La cola local del sidecar queda en:

```text
/var/data/facturas_parseadas/cola
```

FoxPro puede seguir usando `vw_facturas_ocr_pendientes` y `vw_facturas_ocr_detalle`; la vista de pendientes ahora incluye columnas de origen del mail.
