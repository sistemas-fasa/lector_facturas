$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_create_accounting_tables.sh"
$LocalSql = Join-Path (Split-Path $PSScriptRoot -Parent) "facturas_ocr\sql\contabilidad_aprendizaje.sql"
$RemoteScript = "/tmp/server_create_accounting_tables.sh"
$RemoteSql = "/tmp/contabilidad_aprendizaje.sql"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}
if (!(Test-Path $LocalSql)) {
    throw "No existe $LocalSql"
}

Write-Host "Subiendo script a ${HostAlias}:$RemoteScript ..."
scp $LocalScript "${HostAlias}:$RemoteScript"

Write-Host "Subiendo SQL a ${HostAlias}:$RemoteSql ..."
scp $LocalSql "${HostAlias}:$RemoteSql"

Write-Host "Ejecutando creador de tablas en $HostAlias ..."
Write-Host "Te va a pedir los datos de un usuario MySQL con permisos CREATE/VIEW/GRANT."
ssh -t $HostAlias "chmod +x $RemoteScript && SQL_FILE=$RemoteSql bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "La creacion de tablas fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Si arriba aparece 'OK: tablas contables listas', reproba el parser."
