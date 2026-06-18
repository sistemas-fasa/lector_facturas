# Perfiles por Proveedor

## Objetivo

Los perfiles por proveedor permiten mejorar la extracción de campos en facturas
recurrentes sin modificar las reglas globales de parsing. Cada proveedor puede
definir sus propias etiquetas (labels) para total, neto, IVA, percepciones, etc.,
actuando como **fallback controlado** cuando el parsing global no logra extraer
un campo o la confianza es baja.

El sistema **nunca pisa datos confiables** del QR AFIP ni de campos extraídos
con confianza >= 80 en `extraccion_enriquecida`.

## Configuración

### Variable de entorno

```
INVOICE_PROVIDER_PROFILES_FILE=config/provider_profiles.json
```

Si la variable **no está definida**, **está vacía**, apunta a un archivo
**inexistente** o contiene **JSON inválido**, la funcionalidad se desactiva
silenciosamente sin afectar el resto del pipeline.

### Formato JSON

```json
{
  "proveedores": {
    "CUIT_SIN_FORMATO": {
      "nombre": "Nombre del Proveedor",
      "aliases": ["TEXTO EN OCR", "OTRO ALIAS"],
      "campos": {
        "total": ["TOTAL A PAGAR", "IMPORTE TOTAL"],
        "neto_gravado": ["NETO GRAVADO", "SUBTOTAL"],
        "iva_21": ["IVA 21%"],
        "iva_105": ["IVA 10.5%"],
        "percepciones_iibb": ["PERC. IIBB", "PERCEPCION IB"],
        "otros_tributos": ["OTROS TRIBUTOS"],
        "cae": ["CAE", "C.A.E.", "CODIGO AUTORIZACION"],
        "vencimiento_cae": ["VTO CAE", "VENCIMIENTO CAE"]
      },
      "tolerancia_total": "1.00"
    }
  },
  "aliases_globales": {
    "ALIAS GLOBAL": "CUIT_SIN_FORMATO"
  }
}
```

### `proveedores`

Objeto donde cada clave es el CUIT sin formato (solo dígitos) del proveedor.

- `nombre`: Nombre legible (uso en diagnóstico).
- `aliases`: Lista de textos que se buscan en el OCR para identificar al
  proveedor. La búsqueda es *case-insensitive* y por substring.
- `campos`: Mapa de campo a lista de etiquetas a buscar en el OCR. El sistema
  busca el primer número (o fecha/texto) después de la etiqueta.
- `tolerancia_total`: Opcional. Tolerancia para validación cruzada del total
  (por defecto usa `INVOICE_TOTAL_TOLERANCE`).

### `aliases_globales`

Objeto con alias que aplican a cualquier proveedor, evaluados antes de los
aliases específicos de cada proveedor. Sirven para normalizar nombres de
proveedores que aparecen bajo distintas variantes.

## Campos Soportados

| Campo | Extractor | Formato |
|---|---|---|
| `total` | Número decimal | `1234,56` |
| `neto_gravado` | Número decimal | `1234,56` |
| `iva_21` | Número decimal | `1234,56` |
| `iva_105` | Número decimal | `1234,56` |
| `iva_27` | Número decimal | `1234,56` |
| `iva_total` | Número decimal | `1234,56` |
| `percepciones_iibb` | Número decimal | `1234,56` |
| `otros_tributos` | Número decimal | `1234,56` |
| `cae` | Texto | `12345678901234` |
| `vencimiento_cae` | Fecha | `dd/mm/aaaa` |
| `proveedor_nombre` | Texto | Razón social |

## Prioridad de Selección

El perfil se selecciona en este orden:

1. **CUIT del QR AFIP** — si el QR contiene un CUIT que existe en
   `proveedores`, se usa ese perfil.
2. **CUIT detectado en texto** — si el texto OCR/PDF contiene un CUIT
   que coincide con `proveedores`.
3. **Alias global** — si el texto OCR contiene un alias de
   `aliases_globales`. Si hay más de un alias global que coincide, se
   reporta `alias_ambiguo` y no se aplica ningún perfil.
4. **Alias del proveedor** — si el texto OCR contiene un alias de la
   lista `aliases` de algún proveedor. Si hay más de un proveedor
   cuyos alias coinciden, se reporta `alias_ambiguo`.
5. **Sin perfil** — si no hay coincidencia, se continúa sin perfil.

### Ambigüedad

Cuando se detecta ambigüedad (múltiples proveedores coinciden por alias),
el campo `perfil_proveedor_aplicado` se registra como `"alias_ambiguo"` y
no se extraen campos del perfil.

## Regla Clave: No Pisar Datos Confiables

El perfil **solo completa** campos que:

- Están **vacíos** (`""` o `None`) en `extraccion_enriquecida`, o
- Tienen **confianza < 80** en `extraccion_enriquecida`.

Los campos provenientes del QR AFIP (que siempre tienen confianza 100)
**nunca son sobrescritos** por el perfil.

## Registro en la Salida

### `extraccion_enriquecida`

Se agrega el campo:

```json
"perfil_proveedor_aplicado": "qr_cuit"
```

Valores posibles:
- `"qr_cuit"` — seleccionado por CUIT del QR
- `"text_cuit"` — seleccionado por CUIT en texto
- `"alias"` — seleccionado por alias
- `"alias_ambiguo"` — múltiples alias coinciden, no se aplicó perfil
- `"sin_perfil"` — no hay perfil para esta factura
- `null` — funcionalidad desactivada o error de carga

### `diagnostico`

Se agrega:

```json
"perfil_proveedor_usado": "Nombre del Proveedor"
```

Solo cuando se aplicó exitosamente un perfil.

## Depuración

Combinar con `INVOICE_WRITE_DEBUG_TEXTS=true` para ver exactamente qué texto
OCR/PDF se está usando. Los archivos de depuración se escriben en `debug/` e
incluyen tanto el texto combinado como los resultados de extracción enriquecida.

Esto permite ajustar las etiquetas del perfil viendo qué texto real produce
el OCR para una factura determinada.

## Seguridad

**No versionar perfiles reales.** El archivo de perfiles puede contener CUIT,
razón social, y datos fiscales de proveedores reales.

- Los perfiles reales se ignoran por `.gitignore`:
  - `config/provider_profiles.json`
  - `config/provider_profiles.local.json`
- Solo se versiona el archivo ejemplo:
  - `config/provider_profiles.example.json`

Para usar perfiles en producción:
1. Copiar `provider_profiles.example.json` a `provider_profiles.json`
2. Completar con datos reales
3. Configurar `INVOICE_PROVIDER_PROFILES_FILE=config/provider_profiles.json`
4. **Nunca** commitear `provider_profiles.json`
