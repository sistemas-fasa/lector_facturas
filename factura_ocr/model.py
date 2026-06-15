from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class LineItem:
    description: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    subtotal: Decimal | None = None


@dataclass
class IIBBPerception:
    jurisdiction: str = ""
    codjur: str = ""
    amount: Decimal | None = None


@dataclass
class InvoiceData:
    document_type: str = "DESCONOCIDO"
    provider_name: str = ""
    cuit: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    currency: str = "ARS"
    subtotal: Decimal | None = None
    iva: Decimal | None = None
    perceptions_iibb: Decimal | None = None
    perceptions_iibb_detail: list[IIBBPerception] = field(default_factory=list)
    total: Decimal | None = None
    line_items: list[LineItem] = field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    source_file: str = ""
    raw_text: str = ""
    provider_profile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "provider_name": self.provider_name,
            "cuit": self.cuit,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "currency": self.currency,
            "subtotal": str(self.subtotal) if self.subtotal is not None else "",
            "iva": str(self.iva) if self.iva is not None else "",
            "perceptions_iibb": str(self.perceptions_iibb) if self.perceptions_iibb is not None else "",
            "perceptions_iibb_detail": [
                {
                    "jurisdiction": item.jurisdiction,
                    "codjur": item.codjur,
                    "amount": str(item.amount) if item.amount is not None else "",
                }
                for item in self.perceptions_iibb_detail
            ],
            "total": str(self.total) if self.total is not None else "",
            "line_items": [
                {
                    "description": item.description,
                    "quantity": str(item.quantity) if item.quantity is not None else "",
                    "unit_price": str(item.unit_price) if item.unit_price is not None else "",
                    "subtotal": str(item.subtotal) if item.subtotal is not None else "",
                }
                for item in self.line_items
            ],
            "confidence": self.confidence,
            "notes": self.notes,
            "source_file": self.source_file,
            "provider_profile": self.provider_profile,
        }


@dataclass
class ProcessResult:
    ok: bool
    invoice: InvoiceData | None = None
    xml_path: Path | None = None
    dest_path: Path | None = None
    error: str | None = None
    extracted_text: str = ""
    document_kind: str = ""
    provider_key: str = ""
    ai_used: bool = False
