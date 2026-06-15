$ErrorActionPreference = "Stop"

$LocalScript = Join-Path $PSScriptRoot "server_setup_n8n_invoice_parser.sh"
$RemoteScript = "/tmp/server_setup_n8n_invoice_parser.sh"
$HostAlias = "fasa_195"
$TempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("server_setup_n8n_invoice_parser_{0}.sh" -f ([guid]::NewGuid().ToString("N")))

if (!(Test-Path $LocalScript)) {
    throw "No existe $LocalScript"
}

try {
    $scriptText = Get-Content -LiteralPath $LocalScript -Raw -Encoding UTF8
    $scriptText = $scriptText -replace "`r`n", "`n" -replace "`r", "`n"
    [System.IO.File]::WriteAllText($TempScript, $scriptText, [System.Text.UTF8Encoding]::new($false))

    Write-Host "Subiendo script a ${HostAlias}:$RemoteScript ..."
    scp $TempScript "${HostAlias}:$RemoteScript"

    Write-Host "Subiendo helper Python al servidor ..."
    scp `
        (Join-Path $PSScriptRoot "host_invoice_parser_service.py") `
        (Join-Path $PSScriptRoot "invoice_parser_helpers.py") `
        "${HostAlias}:/tmp/"
    ssh $HostAlias "rm -rf /tmp/factura_ocr && mkdir -p /tmp/factura_ocr"
    scp `
        (Join-Path $PSScriptRoot "factura_ocr\__init__.py") `
        (Join-Path $PSScriptRoot "factura_ocr\extract.py") `
        (Join-Path $PSScriptRoot "factura_ocr\model.py") `
        "${HostAlias}:/tmp/factura_ocr/"

    Write-Host "Ejecutando con sudo en $HostAlias ..."
    ssh -t $HostAlias "chmod +x $RemoteScript && sudo bash $RemoteScript"
    if ($LASTEXITCODE -ne 0) {
        throw "El setup remoto fallo con codigo $LASTEXITCODE. Revisa la salida anterior."
    }
}
finally {
    Remove-Item -LiteralPath $TempScript -Force -ErrorAction SilentlyContinue
}

Write-Host "Listo. Si arriba aparece 'OK: invoice-parser sidecar listo para probar desde n8n', avisame y sigo con la prueba del workflow."
