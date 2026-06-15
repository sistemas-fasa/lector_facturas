$ErrorActionPreference = "Stop"

$LocalEnv = Join-Path $PSScriptRoot ".env"
$LocalScript = Join-Path $PSScriptRoot "server_apply_n8n_mysql_env_from_file.sh"
$TempEnv = Join-Path ([System.IO.Path]::GetTempPath()) ("invoice_parser_mysql_{0}.env" -f ([guid]::NewGuid().ToString("N")))
$RemoteScript = "/tmp/server_apply_n8n_mysql_env_from_file.sh"
$RemoteEnv = "/tmp/invoice_parser_mysql.env"
$HostAlias = "fasa_195"
$RequiredKeys = @(
    "FASA_MYSQL_HOST",
    "FASA_MYSQL_PORT",
    "FASA_MYSQL_DATABASE",
    "FASA_MYSQL_USER",
    "FASA_MYSQL_PASSWORD"
)

if (!(Test-Path $LocalEnv)) {
    throw "No existe $LocalEnv"
}
if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

$values = [ordered]@{}
foreach ($line in Get-Content -LiteralPath $LocalEnv -Encoding UTF8) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#") -or !$trimmed.Contains("=")) {
        continue
    }
    $parts = $trimmed.Split("=", 2)
    $key = $parts[0].Trim()
    if ($key.StartsWith("export ")) {
        $key = $key.Substring(7).Trim()
    }
    if ($RequiredKeys -contains $key) {
        $values[$key] = $parts[1].Trim()
    }
}

$missing = @($RequiredKeys | Where-Object { !$values.Contains($_) -or [string]::IsNullOrWhiteSpace($values[$_]) })
if ($missing.Count -gt 0) {
    throw "Faltan variables en ${LocalEnv}: $($missing -join ', ')"
}

try {
    $out = foreach ($key in $RequiredKeys) {
        "$key=$($values[$key])"
    }
    Set-Content -LiteralPath $TempEnv -Value $out -Encoding UTF8

    Write-Host "Variables encontradas en .env local: $($RequiredKeys -join ', ')"
    Write-Host "Subiendo configurador a ${HostAlias}:$RemoteScript ..."
    scp $LocalScript "${HostAlias}:$RemoteScript"

    Write-Host "Subiendo variables MySQL temporales a ${HostAlias}:$RemoteEnv ..."
    scp $TempEnv "${HostAlias}:$RemoteEnv"

    Write-Host "Aplicando configuracion con sudo en $HostAlias ..."
    ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript $RemoteEnv && rm -f $RemoteEnv"
    if ($LASTEXITCODE -ne 0) {
        throw "La aplicacion remota fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
    }
}
finally {
    Remove-Item -LiteralPath $TempEnv -Force -ErrorAction SilentlyContinue
}

Write-Host "Listo. Si arriba aparece 'OK: variables MySQL aplicadas desde .env local', reproba una factura."
