$ErrorActionPreference = "Stop"

$Script = Join-Path $PSScriptRoot "create_n8n_invoice_email_workflow.py"
if (!(Test-Path $Script)) {
    throw "No existe $Script"
}

Push-Location $PSScriptRoot
try {
    python $Script
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo crear/actualizar el workflow de email. Codigo $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
