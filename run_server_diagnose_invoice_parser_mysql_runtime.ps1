$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_diagnose_invoice_parser_mysql_runtime.sh"
$RemoteScript = "/tmp/server_diagnose_invoice_parser_mysql_runtime.sh"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

Write-Host "Subiendo diagnostico a ${HostAlias}:$RemoteScript ..."
scp $LocalScript "${HostAlias}:$RemoteScript"

Write-Host "Ejecutando diagnostico con sudo en $HostAlias ..."
ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "El diagnostico fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Pasame la salida si no termina en OK."
