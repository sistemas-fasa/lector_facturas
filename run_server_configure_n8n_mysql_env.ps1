$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_configure_n8n_mysql_env.sh"
$RemoteScript = "/tmp/server_configure_n8n_mysql_env.sh"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

Write-Host "Subiendo configurador a ${HostAlias}:$RemoteScript ..."
scp $LocalScript "${HostAlias}:$RemoteScript"

Write-Host "Ejecutando configurador con sudo en $HostAlias ..."
Write-Host "Te va a pedir la clave sudo y luego los datos MySQL FASA."
ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "La configuracion remota fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Si arriba aparece 'OK: variables MySQL cargadas en invoice-parser', reproba una factura."
