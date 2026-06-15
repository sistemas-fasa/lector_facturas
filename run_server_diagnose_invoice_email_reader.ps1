$ErrorActionPreference = "Stop"

$HostAlias = "fasa_195"
$LocalScript = Join-Path $PSScriptRoot "server_diagnose_invoice_email_reader.sh"
$RemoteScript = "/tmp/server_diagnose_invoice_email_reader.sh"

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

$TempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("server_diagnose_invoice_email_reader_{0}.sh" -f ([guid]::NewGuid().ToString("N")))

try {
    $scriptText = Get-Content -LiteralPath $LocalScript -Raw -Encoding UTF8
    $scriptText = $scriptText -replace "`r`n", "`n" -replace "`r", "`n"
    [System.IO.File]::WriteAllText($TempScript, $scriptText, [System.Text.UTF8Encoding]::new($false))

    Write-Host "Subiendo diagnostico a ${HostAlias}:$RemoteScript ..."
    scp $TempScript "${HostAlias}:$RemoteScript"

    Write-Host "Ejecutando diagnostico con sudo en $HostAlias ..."
    ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
    if ($LASTEXITCODE -ne 0) {
        throw "El diagnostico fallo con codigo $LASTEXITCODE"
    }
}
finally {
    Remove-Item -LiteralPath $TempScript -Force -ErrorAction SilentlyContinue
}

Write-Host "Listo. Si arriba aparece 'imap_poll_enabled: true' e 'IMAP_OK', el lector esta activo."
