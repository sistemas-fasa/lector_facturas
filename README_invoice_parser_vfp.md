# Parser Facturas PDF Imagen a JSON XML para VFP

Automatizacion n8n para recibir facturas por webhook, email IMAP o Google Drive, extraer texto por OCR y dejar archivos estables para que un ERP Visual FoxPro los consuma sin API.

## Archivos

- `create_n8n_invoice_parser_vfp.py`: crea o actualiza el workflow en n8n y lo deja inactivo.
- `invoice_parser_helpers.py`: normalizacion, extraccion, XML, dedupe auxiliar y escritura atomica.
- `invoice_parser_runtime.py`: puente llamado por el nodo Execute Command.
- `test_webhook_invoice_upload.py`: prueba por webhook con multipart/form-data.
- `vfp_read_invoice_json.prg`: ejemplo VFP para leer `.ready`.

## Paquetes del servidor n8n

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-spa nodejs npm python3
sudo npm install -g imghash
```

Copiar `invoice_parser_helpers.py` e `invoice_parser_runtime.py` a un directorio accesible por el proceso n8n. Si n8n corre en Docker, montarlos dentro del contenedor y ajustar el comando del nodo parser si la ruta no queda en el working directory.

## Variables de entorno

```bash
N8N_API_URL=https://tu-n8n.example.com
N8N_API_KEY=...
INVOICE_OUTPUT_DIR=/var/data/facturas_parseadas
INVOICE_GENERATE_XML=true
DEDUPE_TTL_HOURS=72
DEDUPE_PHASH_HAMMING=12
DRIVE_FOLDER_ID=opcional
IMAP_HOST=opcional
IMAP_PORT=993
IMAP_USER=opcional
IMAP_PASSWORD=opcional
```

## Permisos de carpeta

```bash
sudo mkdir -p /var/data/facturas_parseadas/{originales,errores,duplicados,procesados}
sudo chown -R n8n:n8n /var/data/facturas_parseadas
```

El protocolo de escritura para VFP es:

1. Escribir `.json.tmp`.
2. Escribir `.xml.tmp`.
3. Copiar original en `originales/<sha256>.<ext>`.
4. Renombrar `.tmp` a `.json` y `.xml`.
5. Crear `.ready` vacio.

VFP debe leer solo archivos `.ready`; despues de procesar, mover los archivos a `procesados/` o renombrar `.ready` a `.done`.

## Crear o actualizar workflow

```bash
python create_n8n_invoice_parser_vfp.py
```

El script busca el workflow `Parser Facturas PDF Imagen a JSON XML para VFP`. Si existe lo actualiza; si no existe lo crea; siempre lo deja `active=false`. Si faltan `N8N_API_URL` o `N8N_API_KEY`, genera `workflow_invoice_parser_vfp.preview.json` para inspeccion local.

## Probar webhook

Cuando el workflow este guardado y listo para prueba manual en n8n:

```bash
python test_webhook_invoice_upload.py factura.pdf --url "$N8N_API_URL/webhook-test/facturas/vfp-parser"
```

Con el workflow activo en produccion:

```bash
curl -X POST "$N8N_API_URL/webhook/facturas/vfp-parser" \
  -F "file=@factura.pdf;type=application/pdf"
```

Respuesta esperada:

```json
{
  "status": "OK",
  "json_file": "/var/data/facturas_parseadas/FACTURA_20260611_abcdef12.json",
  "xml_file": "/var/data/facturas_parseadas/FACTURA_20260611_abcdef12.xml",
  "sha256": "abcdef12...",
  "requires_review": true
}
```

## JSON resultante

```json
{
  "version": "1.0",
  "estado": "OK",
  "fecha_proceso": "2026-06-11T10:30:00-03:00",
  "origen": {
    "tipo": "webhook",
    "archivo_original": "factura.pdf",
    "mime_type": "application/pdf",
    "sha256": "...",
    "phash": "...",
    "duplicado": false,
    "motivo_duplicado": null
  },
  "comprobante": {
    "tipo": "FACTURA",
    "letra": "A",
    "punto_venta": "0001",
    "numero": "00001234",
    "fecha_emision": "2026-06-10",
    "fecha_vencimiento": null,
    "moneda": "ARS",
    "cae": null,
    "cae_vencimiento": null
  },
  "emisor": {
    "razon_social": "",
    "cuit": "",
    "iva_condicion": "",
    "domicilio": ""
  },
  "receptor": {
    "razon_social": "",
    "cuit": "",
    "iva_condicion": ""
  },
  "importes": {
    "neto_gravado": 0.0,
    "iva_21": 0.0,
    "iva_105": 0.0,
    "iva_27": 0.0,
    "exento": 0.0,
    "no_gravado": 0.0,
    "percepciones": 0.0,
    "otros_impuestos": 0.0,
    "total": 0.0
  },
  "items": [],
  "ocr": {
    "texto": "...",
    "confianza": 0.85,
    "motor": "tesseract"
  },
  "validaciones": {
    "total_detectado": true,
    "cuit_detectado": true,
    "fecha_detectada": true,
    "numero_detectado": true,
    "requiere_revision": false,
    "observaciones": []
  }
}
```

## Lectura desde Visual FoxPro

Usar `vfp_read_invoice_json.prg` como base:

- Buscar `*.ready` en `INVOICE_OUTPUT_DIR`.
- Calcular el `.json` correspondiente con el mismo nombre base.
- Leer con `FILETOSTR(lcJson)`.
- Si hace falta UTF-8, usar `STRCONV(lcJsonText, 11)`.
- Parsear con la libreria JSON del ERP.
- Procesar campos principales.
- Renombrar `.ready` a `.done` o mover lote a `procesados/`.

## Ajustes pendientes por formatos reales

El parser actual extrae CUIT, fecha, numero, CAE, moneda, total e IVA con reglas conservadoras. Cuando haya muestras reales por proveedor, agregar reglas especificas en `invoice_parser_helpers.py` y probarlas antes de activar el workflow.
