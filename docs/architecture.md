# Arquitectura Híbrida: Extracción Determinística + Fallback IA

## Principio rector

El parser **nunca debe depender de IA generativa** para el flujo principal.
La IA es un recurso opcional, auditable, desactivado por defecto y reservado
exclusivamente para casos donde el pipeline determinístico no alcanza
confianza suficiente.

---

## Flujo principal (determinístico, siempre activo)

```
Correo IMAP / n8n webhook
  → Adjunto (PDF / JPG / PNG)
  → Decodificación QR AFIP (zbarimg / pyzbar)
  → Extracción de texto PDF (pymupdf)
  → OCR con Tesseract (fallback si texto PDF insuficiente)
  → Reglas de extracción locales (factura_ocr/extract.py)
  → Heurísticas de validación cruzada (invoice_parser_helpers.py)
    • QR vs. texto OCR
    • Balance de importes (neto + IVA + percepciones = total)
    • Dígito verificador de CUIT
  → Persistencia en MySQL staging (facturas_ocr_cabecera, detalle, eventos)
  → Consumo desde Visual FoxPro (vistas staging)
```

### Componentes del pipeline determinístico

| Componente | Archivo | Responsabilidad |
|---|---|---|
| Sidecar HTTP | `host_invoice_parser_service.py` | Recibe archivos desde n8n, encola y procesa |
| Decodificador QR | `host_invoice_parser_service.py:633` | Lee código AFIP desde imagen/PDF |
| Extracción de texto | `factura_ocr/extract.py:318` | PDF text + OCR Tesseract |
| Reglas de extracción | `factura_ocr/extract.py:1171` | Parseo de campos: CUIT, fecha, total, IVA, etc. |
| Validación enriquecida | `invoice_parser_helpers.py:590` | Cruzar QR vs OCR, balance, confianza por campo |
| Staging MySQL | `invoice_parser_helpers.py:1069` | Persistencia para FoxPro |

### Salida del pipeline

El resultado es un JSON con trazabilidad por campo:

```json
{
  "version": "2.0",
  "status": "OK",
  "fuentes": { "qr_detectado": true, ... },
  "campos": {
    "proveedor_cuit": {
      "valor": "30-12345678-9",
      "confianza": 98,
      "fuente": "qr",
      "metodo": "qr_cuit",
      "evidencia": "{...}"
    },
    "total": {
      "valor": "1234.56",
      "confianza": 85,
      "fuente": "pdf_text",
      "metodo": "regex_total",
      "evidencia": "TOTAL $ 1234.56"
    }
  },
  "validaciones": {
    "ok": true,
    "fallas": [],
    "balance_importes": { "ok": true }
  }
}
```

Cuando hay campos críticos sin evidencia o validaciones fallidas, el `status`
cambia a `REVIEW_REQUIRED`.

---

## Fallback IA (desactivado por defecto)

### Contrato

- **Nunca reemplaza** datos provenientes del QR AFIP (fuente `"qr"`).
- **Nunca escribe** datos finales en staging sin evidencia registrada.
- Solo se invoca cuando el `status` del pipeline determinístico es
  `REVIEW_REQUIRED` **o** la confianza de un campo crítico está por debajo
  de `INVOICE_AI_MIN_CONFIDENCE`.
- La respuesta de la IA se almacena como un campo más con `fuente: "ai"`,
  método `"ai_generative"` y evidencia del prompt+respuesta.
- La decisión final (aceptar/rechazar sugerencia IA) queda siempre en
  el validador determinístico.

### Variables de entorno (futuras)

```env
# Habilitar fallback IA (false por defecto)
INVOICE_AI_FALLBACK_ENABLED=false

# Invocar IA solo cuando status == REVIEW_REQUIRED
INVOICE_AI_ONLY_WHEN_REVIEW_REQUIRED=true

# Confianza mínima para considerar un campo como "confiable"
# Por debajo de este umbral se invoca IA si está habilitada
INVOICE_AI_MIN_CONFIDENCE=0.85

# Proveedor del modelo (openai, anthropic, etc.)
INVOICE_AI_PROVIDER=openai

# Modelo específico (vacío = usar default del proveedor)
INVOICE_AI_MODEL=
```

### Lugar de inserción en el pipeline

El fallback IA se插aría en `invoice_parser_helpers.py:_build_enriched_extraction`,
**después** del bloque de reglas determinísticas y **antes** de la validación
final, solo si `INVOICE_AI_FALLBACK_ENABLED=true` y se cumplen las condiciones
de guarda.

```
Reglas determinísticas
  → [IA fallback si corresponde]  ← solo aquí
  → Validación y balance
  → Staging MySQL
```

### Ejemplo de campo generado por IA

```json
{
  "proveedor_nombre": {
    "valor": "PROVEEDOR S.A.",
    "confianza": 65,
    "fuente": "ai",
    "metodo": "ai_generative",
    "evidencia": "prompt: extraer razon social de texto OCR\nrespuesta: PROVEEDOR S.A."
  }
}
```

---

## Reglas de seguridad

1. **El QR AFIP siempre tiene prioridad.** Si un campo fue extraído del QR
   (`fuente: "qr"`), ni las reglas determinísticas ni la IA lo sobrescriben.
2. **La IA no persiste por sí misma.** Todo dato sugerido por IA pasa por
   el validador (`_validate_enriched_fields`) antes de llegar a staging.
3. **Auditabilidad.** Cada campo registra `fuente`, `metodo` y `evidencia`.
   Un campo IA siempre contiene el prompt enviado y la respuesta cruda.
4. **Sin secretos en código.** Las claves de API para IA van en `.env` vía
   variables de entorno, no versionadas.
5. **Sin modificación de lógica productiva.** Este documento describe una
   arquitectura futura; el código actual no contiene ninguna invocación a
   un modelo de IA generativa.

---

## Variables de entorno existentes (no IA)

Estas variables ya forman parte del sistema y no se ven afectadas por la
arquitectura híbrida:

| Variable | Uso |
|---|---|
| `INVOICE_TOTAL_TOLERANCE` | Tolerancia en balance de importes |
| `INVOICE_QR_MAX_PAGES` | Páginas a escanear en búsqueda de QR |
| `INVOICE_QR_ZBAR_TIMEOUT_SECONDS` | Timeout para zbarimg |
| `FASA_MYSQL_*` | Conexión a staging MySQL |
| `INVOICE_EMAIL_*` | Configuración de casilla IMAP |

Ver `docs/EMAIL_FACTURAS_OCR.md` para detalle de las variables de email.
