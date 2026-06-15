$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_create_invoice_staging_tables.sh"
$LocalSql = Join-Path $PSScriptRoot "facturas_ocr_staging.sql"
$RemoteScript = "/tmp/server_create_invoice_staging_tables.sh"
$RemoteSql = "/tmp/facturas_ocr_staging.sql"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}
if (!(Test-Path $LocalSql)) {
    throw "No existe $LocalSql"
}

$TempScript = Join-Path $env:TEMP "server_create_invoice_staging_tables_lf.sh"
(Get-Content -Path $LocalScript -Raw).Replace("`r`n", "`n") | Set-Content -Path $TempScript -NoNewline -Encoding utf8

Write-Host "Subiendo script a ${HostAlias}:$RemoteScript ..."
scp $TempScript "${HostAlias}:$RemoteScript"

Write-Host "Subiendo SQL a ${HostAlias}:$RemoteSql ..."
scp $LocalSql "${HostAlias}:$RemoteSql"

Write-Host "Ejecutando creador de tablas staging en $HostAlias ..."
Write-Host "Te va a pedir los datos de un usuario MySQL con permisos CREATE/GRANT."
Write-Host "Para FoxPro podes dejar ENTER si ya manejas permisos por cada usuario del sistema."
ssh -t $HostAlias "chmod +x $RemoteScript && SQL_FILE=$RemoteSql bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "La creacion de tablas staging fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Si arriba aparece 'OK: tablas staging facturas OCR listas', ejecuta el setup del sidecar y reproba una factura."
