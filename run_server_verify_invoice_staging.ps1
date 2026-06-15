$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_verify_invoice_staging.sh"
$RemoteScript = "/tmp/server_verify_invoice_staging.sh"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

$TempScript = Join-Path $env:TEMP "server_verify_invoice_staging_lf.sh"
(Get-Content -Path $LocalScript -Raw).Replace("`r`n", "`n") | Set-Content -Path $TempScript -NoNewline -Encoding utf8

Write-Host "Subiendo verificador staging a ${HostAlias}:$RemoteScript ..."
scp $TempScript "${HostAlias}:$RemoteScript"

Write-Host "Ejecutando verificador staging con sudo en $HostAlias ..."
ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "La verificacion staging fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Si arriba aparece 'OK: verificacion staging finalizada', cabecera/detalle estan accesibles."
