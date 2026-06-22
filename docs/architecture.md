# Arquitectura AI-first: OpenRouter + Validacion Local + Fallback Legacy

## Principio rector

El flujo principal de extraccion de facturas es **AI-first via OpenRouter**.
PyOCR, Tesseract, lectura de texto PDF, QR y regex quedan como fallback legacy.
No se deben seguir optimizando como solucion principal.

```text
n8n -> /enqueue?source_type=email
  -> Adjunto original PDF/JPG/PNG
  -> OpenRouter AI extractor multimodal
  -> Validacion local dura
  -> Si OK: JSON normalizado + staging MySQL
  -> Si falla: fallback legacy actual
  -> Si legacy no alcanza: REVIEW_REQUIRED
```

La IA no decide el estado final. El backend valida y decide.

---

## Ingesta productiva de correo

El contrato de entrada no cambia. n8n sigue leyendo la casilla y enviando el
archivo al sidecar:

```text
POST http://invoice-parser:8765/enqueue?source_type=email
Content-Type: multipart/form-data
Campo obligatorio: file
```

El polling IMAP interno del servicio Python sigue siendo secundario y no debe
activarse en produccion cuando n8n ya lee la casilla.

---

## Pipeline principal

| Etapa | Archivo | Responsabilidad |
|---|---|---|
| Orquestacion HTTP | `host_invoice_parser_service.py` | Recibe archivo y llama primero a `invoice_ai_extractor` |
| Configuracion IA | `invoice_ai_extractor/schema.py` | Lee env vars, valida proveedor/modelo/free models |
| Cliente OpenRouter | `invoice_ai_extractor/openrouter_client.py` | Envia el archivo original como entrada multimodal |
| Prompt | `invoice_ai_extractor/prompts.py` | Instrucciones para JSON fiscal argentino |
| Validacion local | `invoice_ai_extractor/validators.py` | Normaliza y valida campos criticos, importes y confianza |
| Fallback legacy | `host_invoice_parser_service.py` + `invoice_parser_helpers.py` | QR/PDF text/OCR/reglas solo si IA no sirve |
| Staging MySQL | `invoice_parser_helpers.py` | Persistencia para FoxPro |

---

## Variables de entorno

### Desarrollo / staging

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=...
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

`google/gemini-2.5-flash` es el modelo recomendado para validar facturas reales.
`google/gemini-2.5-flash-lite` queda como opcion economica para pruebas
controladas. `openrouter/free` solo sirve como smoke de conectividad, API key y
consumo contra OpenRouter; no se recomienda para facturas reales porque el router
puede seleccionar modelos que omiten campos fiscales criticos como
`punto_venta`, `codigo_afip`, letra o confianza.

### Produccion

```env
INVOICE_AI_ENABLED=true
INVOICE_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=...
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
`INVOICE_AI_ALLOW_FREE_MODELS` no es `true`, la IA no se usa. El error queda registrado como
`free_model_not_allowed` y el flujo cae al fallback legacy.

Criterio de cierre del PR IA-first: el test live debe confirmar que Gemini
Flash levanta `punto_venta`, `codigo_afip`, numero, fecha, CUIT y total en una
factura real. Si no cumple esos campos, antes de seguir tocando prompts hay que
pasar a renderizado de PDF como imagen.

---

## Schema esperado desde IA

La IA debe devolver solo JSON valido:

```json
{
  "proveedor": {"razon_social": null, "cuit": null},
  "comprobante": {
    "tipo": null,
    "letra": null,
    "codigo_afip": null,
    "punto_venta": null,
    "numero": null,
    "fecha_emision": null
  },
  "cae": {"numero": null, "vencimiento": null},
  "importes": {
    "neto_gravado": null,
    "iva_21": null,
    "iva_105": null,
    "iva_27": null,
    "exento": null,
    "no_gravado": null,
    "percepciones": null,
    "percepciones_iibb": null,
    "otros_impuestos": null,
    "total": null
  },
  "moneda": null,
  "confianza": {"general": 0, "campos_dudosos": []},
  "observaciones": []
}
```

---

## Validaciones locales obligatorias

- `punto_venta` no puede ser `0000` y debe ser mayor a cero.
- `numero` no puede ser `00000000`.
- `total` debe ser mayor a cero.
- CUIT se normaliza a digitos y, si existe, debe tener 11 digitos.
- Fecha de emision debe ser valida.
- Si hay CAE, debe tener longitud razonable.
- Si existen componentes de importes y total, la suma debe aproximar al total.
- Si `confianza.general` es menor al minimo configurado, queda
  `REVIEW_REQUIRED`.
- Si faltan o son dudosos campos criticos, queda `REVIEW_REQUIRED`.

Campos criticos:

- proveedor CUIT o razon social;
- tipo/letra/codigo de comprobante;
- punto de venta;
- numero;
- fecha;
- total.

---

## Trazabilidad

`extraccion_enriquecida` conserva la traza AI y el fallback:

```json
{
  "ai": {
    "provider": "openrouter",
    "model": "google/gemini-2.5-flash",
    "fallback_model_used": false,
    "enabled": true,
    "confidence": 0.92,
    "campos_dudosos": [],
    "observaciones": [],
    "raw_response": {},
    "error": null,
    "validaciones": {"ok": true, "fallas": []}
  },
  "fallback_usado": false,
  "fallback_tipo": null,
  "validaciones": {"ok": true, "fallas": []}
}
```

No se guardan claves API en logs ni en JSON generado.
