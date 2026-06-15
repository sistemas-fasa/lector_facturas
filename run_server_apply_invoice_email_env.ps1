$ErrorActionPreference = "Stop"

$HostAlias = "fasa_195"
$LocalScript = Join-Path $PSScriptRoot "server_apply_invoice_email_env_from_file.sh"
$LocalEnv = Join-Path $PSScriptRoot ".env"
$RemoteScript = "/tmp/server_apply_invoice_email_env_from_file.sh"
$RemoteEnv = "/tmp/lector_factura.env"

if (!(Test-Path $LocalScript)) { throw "No existe $LocalScript" }
if (!(Test-Path $LocalEnv)) { throw "No existe $LocalEnv" }

$TempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("server_apply_invoice_email_env_{0}.sh" -f ([guid]::NewGuid().ToString("N")))

try {
    $scriptText = Get-Content -LiteralPath $LocalScript -Raw -Encoding UTF8
    $scriptText = $scriptText -replace "`r`n", "`n" -replace "`r", "`n"
    [System.IO.File]::WriteAllText($TempScript, $scriptText, [System.Text.UTF8Encoding]::new($false))

    Write-Host "Subiendo script a ${HostAlias}:$RemoteScript ..."
    scp $TempScript "${HostAlias}:$RemoteScript"

    Write-Host "Subiendo .env local a ${HostAlias}:$RemoteEnv ..."
    scp $LocalEnv "${HostAlias}:$RemoteEnv"

    Write-Host "Aplicando variables email al invoice-parser ..."
    ssh -t $HostAlias "chmod +x $RemoteScript && sudo LOCAL_ENV_FILE=$RemoteEnv bash $RemoteScript"
    if ($LASTEXITCODE -ne 0) {
        throw "La aplicacion de variables email fallo con codigo $LASTEXITCODE"
    }
}
finally {
    Remove-Item -LiteralPath $TempScript -Force -ErrorAction SilentlyContinue
}

Write-Host "Listo. Si arriba aparece 'OK: variables email aplicadas al invoice-parser', el sidecar ya puede leer la casilla directo."
