from pathlib import Path


def test_vfp_prg_loads_and_shows_iibb_perceptions():
    prg = Path("vfp_facturas_ocr_staging.prg").read_text(encoding="utf-8")

    assert "percepciones_iibb" in prg
    assert "FUNCTION OcrPercepcionesIibb" in prg
    assert "vw_facturas_ocr_percepciones_iibb" in prg
    assert "PROCEDURE OcrMostrarPercepcionesIibb" in prg
    assert "PROCEDURE OcrMostrarFactura" in prg
