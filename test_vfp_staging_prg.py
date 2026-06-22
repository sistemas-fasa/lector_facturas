from pathlib import Path


def test_vfp_prg_loads_and_shows_iibb_perceptions():
    prg = Path("vfp_facturas_ocr_staging.prg").read_text(encoding="utf-8")

    assert "percepciones_iibb" in prg
    assert "FUNCTION OcrPercepcionesIibb" in prg
    assert "vw_facturas_ocr_percepciones_iibb" in prg
    assert "PROCEDURE OcrMostrarPercepcionesIibb" in prg
    assert "PROCEDURE OcrMostrarFactura" in prg


def test_vfp_prg_casts_detail_description_as_char_for_browse():
    prg = Path("vfp_facturas_ocr_staging.prg").read_text(encoding="utf-8")

    assert "comprobante_codigo" in prg
    assert "CAST(descripcion_factura AS CHAR(250)) AS descripcion_factura" in prg
