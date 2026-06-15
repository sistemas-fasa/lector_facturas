$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_bootstrap_accounting_rules.sh"
$LocalSql = Join-Path $PSScriptRoot "bootstrap_accounting_rules_from_stock_co.sql"
$RemoteScript = "/tmp/server_bootstrap_accounting_rules.sh"
$RemoteSql = "/tmp/bootstrap_accounting_rules_from_stock_co.sql"
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

Write-Host "Ejecutando bootstrap de reglas contables en $HostAlias ..."
Write-Host "Te va a pedir un usuario MySQL con permisos INSERT/UPDATE sobre factura_reglas_contables."
ssh -t $HostAlias "chmod +x $RemoteScript && SQL_FILE=$RemoteSql bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "El bootstrap de reglas fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Si arriba aparece 'OK: bootstrap de reglas contables finalizado', reproba el parser."
