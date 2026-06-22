from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from pprint import pformat

import pytest

from invoice_ai_extractor.service import extract_invoice_ai_first


ROOT = Path(__file__).parent


def _load_dotenv_override(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


@pytest.mark.live_openrouter
def test_openrouter_reads_real_invoice_critical_fields_from_dotenv():
    if os.environ.get("RUN_OPENROUTER_LIVE_TESTS") != "1":
        pytest.skip("set RUN_OPENROUTER_LIVE_TESTS=1 to call OpenRouter with a real invoice")

    _load_dotenv_override(ROOT / ".env")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY missing")

    invoice_path = ROOT / "muestras_privadas" / "02.05.2026 - tte hd FACTURA N°23796.pdf"
    result = extract_invoice_ai_first(
        document_bytes=invoice_path.read_bytes(),
        filename=invoice_path.name,
        mime_type=mimetypes.guess_type(invoice_path.name)[0] or "application/pdf",
        sha256="live-openrouter-fixture",
        legacy_extractor=lambda trace: {"estado": "LEGACY_FALLBACK_USED", "trace": trace},
    )

    assert result.trace["fallback_usado"] is False, pformat(result.trace.get("ai", {}))
    assert result.invoice["estado"] == "OK"
    assert result.invoice["comprobante"]["tipo"] == "FACTURA"
    assert result.invoice["comprobante"]["letra"] == "A"
    assert result.invoice["comprobante"]["punto_venta"] == "00024"
    assert result.invoice["comprobante"]["numero"] == "00023796"
    assert result.invoice["comprobante"]["fecha"] == "2026-04-30"
    assert result.invoice["emisor"]["cuit"] == "20-23737702-9"
    assert result.invoice["importes"]["neto_gravado"] == pytest.approx(42903.00)
    assert result.invoice["importes"]["iva_21"] == pytest.approx(9009.63)
    assert result.invoice["importes"]["total"] == pytest.approx(51912.63)
