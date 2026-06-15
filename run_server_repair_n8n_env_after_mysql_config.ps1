$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_repair_n8n_env_after_mysql_config.sh"
$RemoteScript = "/tmp/server_repair_n8n_env_after_mysql_config.sh"
$HostAlias = "fasa_195"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

Write-Host "Subiendo reparador a ${HostAlias}:$RemoteScript ..."
scp $LocalScript "${HostAlias}:$RemoteScript"

Write-Host "Ejecutando reparador con sudo en $HostAlias ..."
ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "La reparacion remota fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
}

Write-Host "Listo. Ahora podes correr de nuevo .\run_server_configure_n8n_mysql_env.ps1"
