$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_diagnose_n8n_invoice_parser.sh"
$RemoteScript = "/tmp/server_diagnose_n8n_invoice_parser.sh"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

Write-Host "Subiendo diagnostico a ${HostAlias}:$RemoteScript ..."
scp $LocalScript "${HostAlias}:$RemoteScript"

Write-Host "Ejecutando diagnostico sin sudo..."
ssh $HostAlias "chmod +x $RemoteScript && bash $RemoteScript"

Write-Host ""
Write-Host "Si ves 'permission denied while trying to connect to the docker API', ejecuta:"
Write-Host "ssh -t $HostAlias 'sudo bash $RemoteScript'"
