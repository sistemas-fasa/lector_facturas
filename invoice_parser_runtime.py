"""Runtime bridge used by the n8n Execute Command node.

It reads OCR text from stdin and writes JSON/XML/original handoff files for VFP.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

from invoice_parser_helpers import atomic_write_files, build_invoice_json


def main(argv: list[str]) -> int:
    if len(argv) != 9:
        print("Uso: invoice_parser_runtime.py source filename mime sha ext output_dir generate_xml ocr_text_path", file=sys.stderr)
        return 2

    _, source_type, filename, mime_type, sha256, extension, output_dir, generate_xml_text, ocr_text_path = argv
    text_path = Path(ocr_text_path)
    ocr_text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else sys.stdin.read()
    invoice = build_invoice_json(
        ocr_text=ocr_text,
        source_type=source_type,
        original_filename=filename,
        mime_type=mime_type,
        sha256=sha256,
        phash="",
    )
    original_path = Path("/tmp/n8n_invoice_parser") / f"{sha256}.{extension}"
    result = atomic_write_files(
        output_dir=output_dir,
        invoice=invoice,
        original_path=original_path if original_path.exists() else None,
        original_extension=extension,
        generate_xml=generate_xml_text.lower() == "true",
        subdir="duplicados" if invoice["estado"] == "DUPLICADO" else None,
    )
    print(json.dumps({
        "status": invoice["estado"],
        "json_file": result["json_file"],
        "xml_file": result["xml_file"],
        "sha256": sha256,
        "requires_review": invoice["validaciones"]["requiere_revision"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
