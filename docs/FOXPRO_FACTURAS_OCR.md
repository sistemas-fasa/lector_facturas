# Integracion FoxPro con facturas OCR

## Objetivo

El parser guarda cada factura procesada en tablas MySQL de staging. Visual FoxPro consume esas tablas por ODBC, muestra facturas pendientes, permite corregir cuentas contables e importa al sistema FASA sin leer JSON.

## Prueba integral

Para crear tablas, desplegar sidecar, subir una factura de prueba y verificar filas en staging:

```powershell
.\run_full_invoice_staging_test.ps1
```

Si las tablas ya existen:

```powershell
.\run_full_invoice_staging_test.ps1 -SkipCreateTables
```

Si el sidecar ya esta desplegado:

```powershell
.\run_full_invoice_staging_test.ps1 -SkipCreateTables -SkipSetup
```

Si el servidor no tiene `mysql-client` y queres que el creador use PyMySQL desde el contenedor:

```powershell
.\run_full_invoice_staging_test.ps1 -SetupFirst
```

## Tablas

- `facturas_ocr_cabecera`: una fila por factura procesada.
- `facturas_ocr_detalle`: una fila por renglon/descripcion de factura.
- `facturas_ocr_eventos`: auditoria operativa.
- `factura_reglas_contables`: reglas de aprendizaje usadas por el parser.
- `vw_facturas_ocr_pendientes`: vista recomendada para la bandeja FoxPro.
- `vw_facturas_ocr_detalle`: vista recomendada para el detalle FoxPro.

## Permisos

El sidecar necesita permisos de escritura sobre staging con el usuario configurado en `FASA_MYSQL_USER`.

FoxPro no necesita un usuario generico si el sistema ya autentica cada operador con su propio usuario MySQL. En ese caso, otorgar estos permisos a cada usuario FoxPro existente o a un rol equivalente:

```sql
GRANT SELECT, UPDATE ON fasa.facturas_ocr_cabecera TO 'usuario_fox'@'%';
GRANT SELECT, UPDATE ON fasa.facturas_ocr_detalle TO 'usuario_fox'@'%';
GRANT SELECT, INSERT ON fasa.facturas_ocr_eventos TO 'usuario_fox'@'%';
GRANT SELECT ON fasa.vw_facturas_ocr_pendientes TO 'usuario_fox'@'%';
GRANT SELECT ON fasa.vw_facturas_ocr_detalle TO 'usuario_fox'@'%';
GRANT SELECT, INSERT, UPDATE ON fasa.factura_reglas_contables TO 'usuario_fox'@'%';
FLUSH PRIVILEGES;
```

## Bandeja de pendientes

```sql
SELECT
  id,
  fecha_proceso,
  proveedor_codigo,
  emisor_razon_social,
  emisor_cuit,
  letra,
  punto_venta,
  numero,
  fecha_emision,
  total,
  requiere_revision,
  observaciones,
  cuenta_contable_sugerida,
  cuenta_descripcion_sugerida,
  score_sugerencia
FROM vw_facturas_ocr_pendientes
ORDER BY requiere_revision DESC, fecha_proceso;
```

## Detalle de una factura

```sql
SELECT
  id,
  factura_id,
  linea,
  descripcion_factura,
  cantidad,
  precio_unitario,
  subtotal,
  cuenta_contable,
  cuenta_descripcion,
  origen_sugerencia,
  score_sugerencia,
  requiere_confirmacion,
  confirmada
FROM vw_facturas_ocr_detalle
WHERE factura_id = ?factura_id
ORDER BY linea;
```

## Confirmar o cambiar cuenta en un renglon

Plantilla disponible: `foxpro_confirmar_cuenta_y_aprender.sql`.

```sql
UPDATE facturas_ocr_detalle
SET cuenta_contable = ?cuenta_contable,
    cuenta_descripcion = ?cuenta_descripcion,
    requiere_confirmacion = 0,
    confirmada = 1,
    usuario_confirmacion = ?usuario,
    fecha_confirmacion = NOW()
WHERE id = ?detalle_id;
```

Registrar auditoria:

```sql
INSERT INTO facturas_ocr_eventos (
  factura_id, detalle_id, sha256, evento, detalle, usuario
)
SELECT
  d.factura_id,
  d.id,
  d.sha256,
  'cuenta_confirmada',
  CONCAT('Cuenta confirmada: ', d.cuenta_contable, ' ', d.cuenta_descripcion),
  ?usuario
FROM facturas_ocr_detalle d
WHERE d.id = ?detalle_id;
```

## Guardar aprendizaje contable

Cuando el usuario cambia una cuenta, FoxPro puede actualizar la regla del proveedor. Para regla por proveedor completo usar `patron_normalizado = ''`. Para regla por descripcion, guardar un patron normalizado, por ejemplo `FLETE`, `HONORARIOS`, `THERMO ESP`.

```sql
INSERT INTO factura_reglas_contables (
  proveedor_codigo,
  proveedor_nombre,
  patron_texto,
  patron_normalizado,
  cuenta_contable,
  cuenta_descripcion,
  importe_min,
  importe_max,
  prioridad,
  confianza,
  requiere_revision,
  activa,
  origen,
  veces_confirmada,
  ultima_confirmacion,
  created_at,
  updated_at
)
SELECT
  c.proveedor_codigo,
  c.proveedor_nombre,
  ?patron_texto,
  ?patron_normalizado,
  d.cuenta_contable,
  d.cuenta_descripcion,
  0,
  0,
  100,
  100,
  0,
  1,
  'manual',
  1,
  NOW(),
  NOW(),
  NOW()
FROM facturas_ocr_detalle d
JOIN facturas_ocr_cabecera c ON c.id = d.factura_id
WHERE d.id = ?detalle_id
ON DUPLICATE KEY UPDATE
  cuenta_contable = VALUES(cuenta_contable),
  cuenta_descripcion = VALUES(cuenta_descripcion),
  confianza = 100,
  requiere_revision = 0,
  activa = 1,
  origen = 'manual',
  veces_confirmada = veces_confirmada + 1,
  ultima_confirmacion = NOW(),
  updated_at = NOW();
```

## Marcar factura importada

Plantilla disponible: `foxpro_marcar_factura_importada.sql`.

Luego de grabarla correctamente en el sistema:

```sql
UPDATE facturas_ocr_cabecera
SET importada = 1,
    fecha_importacion = NOW(),
    usuario_importacion = ?usuario,
    observaciones_importacion = ?observaciones
WHERE id = ?factura_id;
```

Evento recomendado:

```sql
INSERT INTO facturas_ocr_eventos (
  factura_id, sha256, evento, detalle, usuario
)
SELECT
  id,
  sha256,
  'factura_importada',
  'Factura importada en Visual FoxPro',
  ?usuario
FROM facturas_ocr_cabecera
WHERE id = ?factura_id;
```

## Criterio operativo

FoxPro deberia permitir importar directamente solo cuando:

- `requiere_revision = 0`
- `importada = 0`
- los renglones tienen cuenta contable

Si el usuario cambia una cuenta, conviene guardar aprendizaje solo cuando confirma explicitamente la correccion. No conviene aprender automaticamente de facturas no revisadas.
