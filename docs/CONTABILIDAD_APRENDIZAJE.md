# Aprendizaje de cuentas contables en lector_factura

Fecha: 2026-06-11

Este documento deja respaldado el criterio de funcionamiento para la sugerencia y aprendizaje de cuentas contables del flujo `lector_factura`.

## Estado actual

El parser sugiere una cuenta contable, pero todavia no aprende automaticamente cuando una persona corrige o confirma una imputacion.

El flujo actual es:

1. Extrae datos de la factura.
2. Busca el proveedor en `proveedo` por CUIT o por nombre.
3. Si encuentra una regla activa en `factura_reglas_contables`, usa esa cuenta.
4. Si no hay regla, consulta el historico de `stock_co` y toma la cuenta mas usada para ese proveedor.
5. Si no hay proveedor, no hay historico, o el score es bajo, devuelve `requiere_confirmacion: true`.

La salida contable queda en el bloque JSON `contabilidad`, por ejemplo:

```json
{
  "proveedor_codigo": "2287",
  "proveedor_nombre": "LUIS EDGARDO STEFFEN",
  "cuenta_contable": "500200",
  "cuenta_descripcion": "ALQUILERES PAGADOS",
  "origen_sugerencia": "historico_stock_co",
  "score_sugerencia": 97.99,
  "requiere_confirmacion": false,
  "observaciones": []
}
```

## Fuentes de informacion

Hay dos niveles de historico:

- Historico real de FASA: se actualiza cuando las facturas se cargan y terminan reflejadas en tablas operativas como `stock_co`. El parser lo usa solo como lectura.
- Historico de aprendizaje del parser: se guarda en tablas nuevas de soporte, pensadas para registrar confirmaciones y reglas aprendidas.

Tablas de aprendizaje:

- `factura_ejemplos_contables`: guarda casos confirmados por una persona.
- `factura_reglas_contables`: guarda reglas reutilizables por proveedor, patron de texto, importe, periodicidad y cuenta.
- `vw_factura_reglas_contables_activas`: vista para consultar reglas vigentes.

## Proveedor nuevo

Para un proveedor nuevo, el comportamiento esperado es:

1. El proveedor debe existir en `proveedo`.
2. La primera factura puede extraerse, pero la cuenta debe confirmarse manualmente si no hay historico confiable.
3. La confirmacion se guarda en `factura_ejemplos_contables`.
4. Si corresponde, se crea o actualiza una regla en `factura_reglas_contables`.
5. Las proximas facturas del mismo proveedor ya pueden sugerirse por regla.

Si el proveedor no existe en `proveedo`, el parser puede devolver datos de la factura, pero la imputacion contable queda sin proveedor vinculado y debe revisarse.

## Recomendacion operativa

No conviene hacer aprendizaje 100% automatico sin confirmacion humana.

Regla recomendada:

- Score alto, por ejemplo `>= 90`: sugerir cuenta con baja friccion.
- Score medio o bajo: marcar `requiere_confirmacion: true`.
- Cuenta corregida o confirmada por el usuario: guardar aprendizaje.

Esto evita que una mala lectura OCR o una factura atipica contamine las reglas futuras.

## Permisos MySQL recomendados

El usuario actual de inferencia puede ser solo lectura para consultar:

- `proveedo`
- `stock_co`
- `contable`
- `factura_reglas_contables`
- `vw_factura_reglas_contables_activas`

Para aprendizaje conviene crear otro usuario con permisos limitados, por ejemplo `invoice_parser_learning`.

Ese usuario deberia tener:

- Lectura sobre `proveedo`, `stock_co` y `contable`.
- Lectura y escritura solo sobre `factura_ejemplos_contables` y `factura_reglas_contables`.
- Sin permisos de escritura sobre tablas operativas de FASA como `stock_co` o `proveedo`.

## Proximo paso propuesto

Crear un endpoint o flujo de n8n para confirmar imputaciones. La entrada minima podria ser:

```json
{
  "hash_documento": "...",
  "proveedor_codigo": "2287",
  "cuenta_confirmada": "500200",
  "confirmado_por": "oscar"
}
```

Ese flujo deberia:

1. Validar que el proveedor exista.
2. Validar que la cuenta exista en `contable`.
3. Insertar o actualizar un ejemplo en `factura_ejemplos_contables`.
4. Insertar o actualizar una regla en `factura_reglas_contables` cuando el caso sea repetible.
5. Mantener `requiere_revision` activo cuando la regla tenga baja confianza o sea nueva.

## Bootstrap inicial desde stock_co

Para no empezar con las tablas de reglas vacias, se puede hacer una carga inicial desde el historico de `stock_co`.

El criterio recomendado es conservador:

- Crear una regla solo cuando el proveedor tiene una cuenta claramente dominante.
- Usar por defecto `score >= 95`.
- Exigir al menos 3 lineas historicas para el proveedor.
- Crear una regla general por proveedor, sin patron de texto, cuando casi todo su historico fue a la misma cuenta.
- Dejar proveedores mixtos fuera del bootstrap para que pasen por confirmacion manual.

Scripts disponibles:

```powershell
.\run_server_bootstrap_accounting_rules.ps1
```

Ese script:

1. Sube al servidor el SQL `bootstrap_accounting_rules_from_stock_co.sql`.
2. Muestra una vista previa por rango de confianza.
3. Informa cuantas reglas se cargarian con el umbral elegido.
4. Pide confirmacion escribiendo `SI`.
5. Inserta o actualiza reglas en `factura_reglas_contables` con origen `historico_stock_co`.

En la revision inicial del historico habia 1200 proveedores con lineas contables en `stock_co`; 883 proveedores tenian una cuenta dominante con score `>= 95`.

## Criterio de seguridad

El parser y n8n no deben tener permisos amplios sobre la base productiva. La automatizacion debe aprender en tablas auxiliares y sugerir imputaciones, no modificar movimientos contables reales sin una etapa explicita de confirmacion.
