"""Prompts for Argentine invoice extraction via multimodal AI."""

SYSTEM_PROMPT = """Extrae los datos fiscales de esta factura argentina.

Responde unicamente JSON valido compatible con el schema indicado.
No agregues texto fuera del JSON.
No inventes datos.
Si un campo no se ve con claridad, devuelve null y agregalo en confianza.campos_dudosos.
Normaliza CUIT solo con digitos.
Normaliza importes con punto decimal, sin separador de miles.
Normaliza punto_venta con 4 digitos.
punto_venta no puede ser 0000.
Normaliza numero con 8 digitos.
Detecta tipo de comprobante: Factura A/B/C, Nota de Credito, Nota de Debito u otro.
Detecta codigo AFIP si esta visible.
Detecta CAE y vencimiento CAE si estan visibles.
No calcules ni completes campos fiscales que no esten visibles.
Si hay diferencias entre importes visibles, informalo en observaciones.
"""

USER_SCHEMA_PROMPT = """Devuelve solo este JSON:
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
"""
