from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .prompts import SYSTEM_PROMPT, USER_SCHEMA_PROMPT


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class OpenRouterClient:
    api_key: str

    def extract_invoice(
        self,
        *,
        document_bytes: bytes,
        filename: str,
        mime_type: str,
        model: str,
        timeout_seconds: int,
    ) -> str:
        try:
            return self._post_completion(
                document_parts=[_document_part(document_bytes, filename, mime_type)],
                model=model,
                timeout_seconds=timeout_seconds,
            )
        except urllib.error.HTTPError as exc:
            if not _looks_like_pdf_unsupported(exc) or not (mime_type or "").lower().startswith("application/pdf"):
                raise
            image_parts = _pdf_image_parts(document_bytes)
            return self._post_completion(document_parts=image_parts, model=model, timeout_seconds=timeout_seconds)

    def _post_completion(self, *, document_parts: list[dict[str, Any]], model: str, timeout_seconds: int) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": USER_SCHEMA_PROMPT}, *document_parts]},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/sistemas-fasa/lector_facturas",
                "X-OpenRouter-Title": "FASA Invoice AI Extractor",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except TimeoutError:
            raise
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(text_parts)
        return str(content or "")


def _document_part(document_bytes: bytes, filename: str, mime_type: str) -> dict[str, Any]:
    guessed_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data_url = f"data:{guessed_type};base64,{base64.b64encode(document_bytes).decode('ascii')}"
    if guessed_type.startswith("image/"):
        return {"type": "image_url", "image_url": {"url": data_url}}
    return {
        "type": "file",
        "file": {
            "filename": Path(filename or "factura").name,
            "file_data": data_url,
        },
    }


def _pdf_image_parts(document_bytes: bytes, *, max_pages: int = 2) -> list[dict[str, Any]]:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(f"pymupdf no disponible para convertir PDF a imagen: {exc}") from exc

    parts: list[dict[str, Any]] = []
    doc = fitz.open(stream=document_bytes, filetype="pdf")
    try:
        for page_index in range(min(max_pages, len(doc))):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            png_bytes = pix.tobytes("png")
            data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
    finally:
        doc.close()
    if not parts:
        raise RuntimeError("PDF sin paginas renderizables para OpenRouter")
    return parts


def _looks_like_pdf_unsupported(exc: urllib.error.HTTPError) -> bool:
    try:
        body = exc.read().decode("utf-8", errors="ignore").lower()
    except Exception:
        body = ""
    return "pdf" in body and ("unsupported" in body or "not supported" in body)
