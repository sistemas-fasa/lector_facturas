param(
    [string]$InvoiceDir = "F:\apps\LISTAS\0000 - FACTURAS WHATSAPP",
    [string]$InvoiceGlob = "01.04.2026 - IAA.jpeg",
    [int]$Limit = 1,
    [switch]$SkipCreateTables,
    [switch]$SkipSetup,
    [switch]$SetupFirst
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "== prueba integral staging facturas OCR =="
Write-Host "Proyecto: $PSScriptRoot"
Write-Host "InvoiceDir: $InvoiceDir"
Write-Host "InvoiceGlob: $InvoiceGlob"

if ($SetupFirst -and !$SkipSetup) {
    Write-Host ""
    Write-Host "== 1/5 redesplegar sidecar invoice-parser antes de crear tablas =="
    Write-Host "Usar esto si el servidor no tiene mysql-client y el creador debe apoyarse en PyMySQL del container."
    & (Join-Path $PSScriptRoot "run_server_setup_n8n_invoice_parser.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo el setup inicial del sidecar."
    }
}

if (!$SkipCreateTables) {
    Write-Host ""
    Write-Host "== crear/verificar tablas staging =="
    Write-Host "Nota: este paso puede pedir usuario/clave MySQL con permisos CREATE/GRANT."
    & (Join-Path $PSScriptRoot "run_server_create_invoice_staging_tables.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo la creacion de tablas staging."
    }
}
else {
    Write-Host "== crear/verificar tablas staging: omitido =="
}

if (!$SkipSetup -and !$SetupFirst) {
    Write-Host ""
    Write-Host "== redesplegar sidecar invoice-parser =="
    & (Join-Path $PSScriptRoot "run_server_setup_n8n_invoice_parser.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo el setup del sidecar."
    }
}
else {
    Write-Host "== redesplegar sidecar invoice-parser: omitido o ya ejecutado =="
}

Write-Host ""
Write-Host "== probar factura contra webhook =="
python (Join-Path $PSScriptRoot "test_batch_invoice_upload.py") `
    --dir $InvoiceDir `
    --glob $InvoiceGlob `
    --limit $Limit `
    --keep-active
if ($LASTEXITCODE -ne 0) {
    throw "Fallo la prueba batch del webhook."
}

Write-Host ""
Write-Host "== verificar filas staging en MySQL =="
& (Join-Path $PSScriptRoot "run_server_verify_invoice_staging.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Fallo la verificacion staging."
}

Write-Host ""
Write-Host "OK: prueba integral staging facturas OCR finalizada"
