"""AI-first invoice extraction package.

The legacy PDF text / QR / OCR path remains outside this package and is used
only as fallback by the orchestration service.
"""

from .service import AiFirstResult, extract_invoice_ai_first

__all__ = ["AiFirstResult", "extract_invoice_ai_first"]
