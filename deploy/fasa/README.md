# FASA - Despliegue productivo de invoice-parser

Este directorio contiene plantillas y documentacion especifica para el
despliegue del servicio `invoice-parser` en el servidor productivo de FASA
(`fasa_195`).

## Por que existe este directorio

El `docker-compose.yml` productivo **no esta versionado en ningun repo**; solo
vive en el servidor. Esto significa que cambios de configuracion critica
(como deshabilitar el polling IMAP) pueden perderse si el servidor se
reconstruye o el compose se sobrescribe.

Estos archivos son la **version de referencia** de la configuracion productiva
de FASA. Sirven como checklist de deploy y documentacion de por que ciertos
valores son obligatorios.

## Incidente que origino esta documentacion

**Problema:** El servidor FASA presentaba errores `ECONNRESET` en la conexion
IMAP con Ferozo.

**Causa raiz:** Dos conexiones IMAP simultaneas al mismo buzón
`facturas.ocr@fasa.ar`:

| Conexion | Quien | Tipo | Frecuencia |
|---|---|---|---|
| 1 | n8n (trigger `emailReadImap`) | IDLE persistente | Tiempo real |
| 2 | invoice-parser (`imap_poll_worker`) | Polling | Cada 60 segundos |

Ferozo corta conexiones IMAP concurrentes o excesivas con `ECONNRESET`.

**Solucion:** `INVOICE_EMAIL_IMAP_POLL_ENABLED=false` en el servicio
`invoice-parser`.

**Leccion:** El polling IMAP interno del sidecar Python **no debe activarse**
en produccion si n8n ya esta leyendo la misma casilla por IMAP. Esta
documentacion versiona esa decision.

## Archivos

| Archivo | Proposito |
|---|---|
| `.env.invoice-parser.example` | Variables de entorno del servicio con defaults seguros para FASA |
| `docker-compose.invoice-parser.snippet.yml` | Bloque de servicio docker-complete listo para copiar al compose productivo |

## Reglas de deploy

1.  **`INVOICE_EMAIL_IMAP_POLL_ENABLED=false` siempre** en produccion con n8n.
2.  No versionar `.env` real ni secretos (passwords, tokens, API keys).
3.  Los valores de IMAP en `.env.example` son solo documentacion; n8n maneja
    el correo en FASA.
4.  Mantener este directorio sincronizado si cambia la configuracion
    productiva.

## Referencias

- Issue #18: https://github.com/sistemas-fasa/lector_facturas/issues/18
- `docs/n8n-email-ingestion.md` — Contrato completo de ingesta de correo
- `README.md` raiz — Vision general del proyecto
