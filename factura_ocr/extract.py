from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import fitz  # pymupdf
except Exception:  # pragma: no cover - optional when parsing already extracted text
    fitz = None
try:
    from PIL import Image, ImageFilter, ImageOps
except Exception:  # pragma: no cover - optional when parsing already extracted text
    Image = None
try:
    import pytesseract
except Exception:  # pragma: no cover - optional when parsing already extracted text
    pytesseract = None

from .model import IIBBPerception, InvoiceData, LineItem

TEXT_THRESHOLD = 40
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

PROVINCES = [
    "BUENOS AIRES",
    "CABA",
    "CIUDAD AUTONOMA DE BUENOS AIRES",
    "CATAMARCA",
    "CHACO",
    "CHUBUT",
    "CORDOBA",
    "CORRIENTES",
    "ENTRE RIOS",
    "FORMOSA",
    "JUJUY",
    "LA PAMPA",
    "LA RIOJA",
    "MENDOZA",
    "MISIONES",
    "NEUQUEN",
    "RIO NEGRO",
    "SALTA",
    "SAN JUAN",
    "SAN LUIS",
    "SANTA CRUZ",
    "SANTA FE",
    "SANTIAGO DEL ESTERO",
    "TIERRA DEL FUEGO",
    "TUCUMAN",
]

JURISDICTION_CODJUR = {
    "MISIONES": "914",
    "CORRIENTES": "905",
    "ENTRE RIOS": "908",
    "BUENOS AIRES": "902",
    "CABA": "901",
    "CAPITAL FEDERAL": "901",
    "CIUDAD AUTONOMA DE BUENOS AIRES": "901",
    "FORMOSA": "909",
    "CHACO": "906",
    "SANTA FE": "921",
    "CORDOBA": "904",
    "SANTIAGO DEL ESTERO": "0",
    "TUCUMAN": "0",
    "JUJUY": "910",
    "SALTA": "0",
    "LA RIOJA": "912",
    "CATAMARCA": "903",
    "SAN JUAN": "0",
    "MENDOZA": "0",
    "SAN LUIS": "919",
    "LA PAMPA": "911",
    "NEUQUEN": "0",
    "RIO NEGRO": "0",
    "CHUBUT": "907",
    "SANTA CRUZ": "0",
    "TIERRA DEL FUEGO": "0",
}

JURISDICTION_ALIASES = {
    "CAPITAL FEDERAL": "CABA",
    "CABA": "CABA",
    "CIUDAD AUTONOMA DE BUENOS AIRES": "CABA",
    "MISION": "MISIONES",
    "MNES": "MISIONES",
}


def normalize_text(text: str) -> str:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _lines(text: str) -> list[str]:
    return [line.strip() for line in normalize_text(text).splitlines() if line.strip()]


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(" ", "")
    # Prefer the last amount-like token on the line so labels such as
    # "IVA 21% 611098.49" resolve to 611098.49 instead of a mangled value.
    tokens = re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", s)
    if not tokens:
        return None
    s = tokens[-1]
    if re.search(r"\d+,\d+", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _valid_cuit_digits(digits: str) -> bool:
    if not re.fullmatch(r"\d{11}", digits or ""):
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(digit) * weight for digit, weight in zip(digits[:10], weights))
    remainder = 11 - (total % 11)
    check = 0 if remainder == 11 else 9 if remainder == 10 else remainder
    return check == int(digits[-1])


def _fmt_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def _parse_date_token(text: str) -> str:
    m = re.search(r"\b(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})\b", text)
    return _fmt_date(m.group(1)) if m else ""


def _normalize_label_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.upper()


def is_supported_document(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def extract_pdf_text(pdf_path: Path) -> str:
    if fitz is None:
        raise RuntimeError("pymupdf no disponible para leer PDF")
    doc = fitz.open(pdf_path)
    parts = []
    for page in doc:
        parts.append(_extract_page_text_by_position(page))
    return normalize_text("\n".join(parts))


def _extract_page_text_by_position(page: Any) -> str:
    words = page.get_text("words")
    if not words:
        return page.get_text("text")

    rows: list[list[Any]] = []
    for word in sorted(words, key=lambda w: (w[1], w[0])):
        y0 = float(word[1])
        for row in rows:
            row_y = sum(float(w[1]) for w in row) / len(row)
            if abs(row_y - y0) <= 3.5:
                row.append(word)
                break
        else:
            rows.append([word])

    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda w: float(w[0]))
        parts: list[str] = []
        last_x1: float | None = None
        for word in row:
            x0 = float(word[0])
            x1 = float(word[2])
            text = str(word[4])
            if last_x1 is not None:
                gap = x0 - last_x1
                if gap > 42:
                    parts.append("    ")
                elif gap > 8:
                    parts.append("  ")
                else:
                    parts.append(" ")
            parts.append(text)
            last_x1 = x1
        lines.append("".join(parts).strip())
    return "\n".join(line for line in lines if line)


# Backwards-compatible alias for older imports and tests.
def extract_text_from_pdf(pdf_path: Path) -> str:
    return extract_pdf_text(pdf_path)


def _page_to_image(page: fitz.Page, dpi: int = 220) -> Image.Image:
    if fitz is None or Image is None:
        raise RuntimeError("pymupdf/pillow no disponibles para convertir PDF a imagen")
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    mode = "RGB" if pix.n < 5 else "CMYK"
    img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
    if mode == "CMYK":
        img = img.convert("RGB")
    return img


def ocr_image(img: Image.Image, lang: str = "spa+eng") -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract no disponible para OCR de imagen")
    variants = [img]
    try:
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray)
        gray = gray.resize((gray.width * 3, gray.height * 3))
        gray = gray.filter(ImageFilter.SHARPEN)
        variants.append(gray)
    except Exception:
        pass

    outputs: list[str] = []
    configs = [
        "--oem 3 --psm 6 -c preserve_interword_spaces=1",
        "--oem 3 --psm 11",
    ]
    for variant in variants:
        for config in configs:
            try:
                text = pytesseract.image_to_string(variant, lang=lang, config=config)
            except TypeError:
                text = pytesseract.image_to_string(variant, lang=lang)
            except Exception:
                continue
            if text.strip():
                outputs.append(text)

    if not outputs:
        return ""
    return max((normalize_text(output) for output in outputs), key=_ocr_quality_score)


def _ocr_quality_score(text: str) -> int:
    normalized = _normalize_label_text(text)
    score = 0
    score += min(len(text) // 80, 40)
    if re.search(r"\bFACTURA\b", normalized):
        score += 15
    if re.search(r"\b[A]\b[\s\S]{0,120}\bFACTURA\b|\bFACTURA\b[\s\S]{0,120}\b[A]\b", normalized):
        score += 8
    if re.search(r"\b\d{4,5}\s*[-–]\s*\d{6,10}\b", text):
        score += 15
    if re.search(r"\bC\.?\s*A\.?\s*E\.?\b|\bCAE\b", normalized):
        score += 10
    if re.search(r"\bTOTAL\b[\s:]*\$?\s*\d", normalized):
        score += 10
    if re.search(r"\bSUBTOTAL\b[\s:]*\$?\s*\d", normalized):
        score += 8
    if re.search(r"\bC[UÜVLI1|]{1,3}T\b", normalized):
        score += 8
    valid_cuits = sum(1 for candidate in re.findall(r"\b\d{2}[- ]?\d{8}[- ]?\d\b|\b\d{11}\b", text) if _valid_cuit_digits(re.sub(r"\D", "", candidate)))
    score += valid_cuits * 12
    if re.search(r"\b(CANTIDAD|DESCRIPCI[OÓ]N|PRECIO|TOTAL)\b", normalized):
        score += 8
    if re.search(r"\bM2\b|\bTHERMO\b|\bFACTURA\s+ANTICIPADA\b", normalized):
        score += 8
    score -= len(re.findall(r"[{}|_]{2,}|[^\w\s.,:/;()%$#º°ÁÉÍÓÚÜÑáéíóúüñ-]", text)) // 6
    return score


def extract_pdf_ocr(pdf_path: Path, lang: str = "spa+eng") -> str:
    doc = fitz.open(pdf_path)
    parts = []
    for page in doc:
        img = _page_to_image(page)
        parts.append(ocr_image(img, lang=lang))
    return normalize_text("\n".join(parts))


def extract_image_text(image_path: Path, lang: str = "spa+eng") -> str:
    if Image is None:
        raise RuntimeError("pillow no disponible para leer imagen")
    with Image.open(image_path) as img:
        return ocr_image(img, lang=lang)


def read_document_text(path: Path, lang: str = "spa+eng") -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = extract_pdf_text(path)
        if len(text) >= TEXT_THRESHOLD:
            return text, "pdf_text"
        return extract_pdf_ocr(path, lang=lang), "pdf_ocr"
    return extract_image_text(path, lang=lang), "image_ocr"


def looks_like_credit_or_debit(text: str) -> str:
    lines = _lines(text)

    # Prefer explicit labels in the top portion of the document, line by line.
    for line in lines[:25]:
        upper = line.upper().strip()
        if re.fullmatch(r"FACTURA(?:\s+[A-Z0-9./-]+)?", upper):
            return "FACTURA"
        if re.fullmatch(r"NOTA\s+DE\s+CR[ÉE]DITO(?:\s+[A-Z0-9./-]+)?", upper):
            return "NOTA_CREDITO"
        if re.fullmatch(r"NOTA\s+DE\s+D[ÉE]BITO(?:\s+[A-Z0-9./-]+)?", upper):
            return "NOTA_DEBITO"

    full_text = text.upper()
    if re.search(r"\bNOTA\s+DE\s+CR[ÉE]DITO\b", full_text):
        return "NOTA_CREDITO"
    if re.search(r"\bNOTA\s+DE\s+D[ÉE]BITO\b", full_text):
        return "NOTA_DEBITO"
    if re.search(r"\bFACTURA\b", full_text):
        return "FACTURA"
    return "DESCONOCIDO"


def guess_provider_key(text: str) -> str:
    lines = _lines(normalize_text(text))
    candidate = _extract_provider_name(lines, {}, "")
    if candidate:
        return re.sub(r"[^0-9A-ZÁÉÍÓÚÑÜa-záéíóúñü]+", "_", candidate.strip().upper())[:80]

    patterns = [
        r"C\.?U\.?I\.?T\.?[:\s]+([0-9\-]{10,13})",
        r"Raz[oó]n Social[:\s]+(.+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return re.sub(r"[^0-9A-ZÁÉÍÓÚÑÜa-záéíóúñü]+", "_", m.group(1).strip().upper())[:80]
    return "UNKNOWN"


def _normalized_profile(provider_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = provider_profile or {}
    return {
        "aliases": list(profile.get("aliases", []) or []),
        "cuit": str(profile.get("cuit", "") or ""),
        "known_labels": list(profile.get("known_labels", []) or []),
        "document_types": list(profile.get("document_types", []) or []),
    }


def _extract_provider_name(lines: list[str], profile: dict[str, Any], provider_key: str) -> str:
    aliases = [alias.strip() for alias in profile.get("aliases", []) if str(alias).strip()]
    text = "\n".join(lines)

    for alias in aliases:
        if re.search(rf"\b{re.escape(alias)}\b", text, flags=re.I):
            return alias

    customer_markers = [
        r"apellid[oó]\s+y\s+nombre\s*/\s*raz[oó]n\s+social",
        r"sr\s*\(es\)",
        r"sr/es",
        r"cliente",
        r"domicilio\s+comercial",
    ]
    issuer_lines: list[str] = []
    for idx, line in enumerate(lines):
        if any(re.search(marker, line, flags=re.I) for marker in customer_markers):
            issuer_lines = lines[:idx]
            break
    if not issuer_lines:
        issuer_lines = lines[:20]

    issuer_text = "\n".join(issuer_lines)

    label_patterns = [
        r"(?:RAZ[ÓO]N SOCIAL|PROVEEDOR|EMISOR|TITULAR)[:\s-]*([^\n]+)",
    ]
    for pattern in label_patterns:
        m = re.search(pattern, issuer_text, flags=re.I)
        if m:
            candidate = m.group(1).strip()
            candidate = re.split(r"\s{2,}|CUIT|DNI|IVA|CONDICIÓN|CONDICION|DOMICILIO", candidate, maxsplit=1, flags=re.I)[0].strip()
            candidate = candidate.strip(" :-")
            if candidate and sum(ch.isalpha() for ch in candidate) >= 4:
                return candidate

    company_markers = [
        r"\bS\.?A\.?S?\b",
        r"\bS\.?R\.?L\.?\b",
        r"SOCIEDAD\s+ANONIMA",
        r"SOCIEDAD\s+DE\s+RESPONSABILIDAD\s+LIMITADA",
        r"SOCIEDAD\s+POR\s+ACCIONES\s+SIMPLIFICADA",
        r"S\.A\.",
        r"S\.R\.L\.",
    ]
    skip_words = {
        "ORIGINAL",
        "COPIA",
        "DUPLICADO",
        "TRIPLICADO",
        "FACTURA",
        "NOTA DE CRÉDITO",
        "NOTA DE CREDITO",
        "NOTA DE DÉBITO",
        "NOTA DE DEBITO",
        "CAE",
        "COMPROBANTE",
        "AUTORIZADO",
        "PÁG",
        "PAG",
        "FECHA",
        "VENCE",
        "VTO",
        "TOTAL",
        "SUBTOTAL",
        "IVA",
        "CUIT",
        "DOMICILIO",
        "TEL",
        "TEL.",
    }

    scored: list[tuple[int, str]] = []
    for line in issuer_lines[:20]:
        candidate = line.strip().strip("•·-–—")
        upper = candidate.upper()
        if len(upper) < 4:
            continue
        if re.fullmatch(r"[0-9./\-]+", upper):
            continue
        if any(word == upper or word in upper for word in skip_words):
            continue
        if sum(ch.isalpha() for ch in candidate) < 4:
            continue
        score = 0
        if any(re.search(marker, upper, flags=re.I) for marker in company_markers):
            score += 30
        if upper == candidate and any(ch.isalpha() for ch in candidate):
            score += 8
        if 2 <= len(candidate.split()) <= 8:
            score += 4
        if any(ch.isdigit() for ch in candidate):
            score -= 4
        if re.search(r"\b(?:AV|CALLE|RUTA|PASAJE|DIRECCION|DIRECCIÓN|HUERGO|QUIROGA|CORDOBA|CORDOBA|PUERTO|MISIONES)\b", upper):
            score -= 6
        scored.append((score, candidate))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored[0][0] > 0:
            return scored[0][1]

    if provider_key and provider_key != "UNKNOWN":
        return provider_key.replace("_", " ").title()
    return ""


def _extract_cuit(text: str, profile: dict[str, Any]) -> str:
    cuit = str(profile.get("cuit", "") or "").strip()
    if cuit:
        digits = re.sub(r"\D", "", cuit)
        if _valid_cuit_digits(digits):
            return digits

    patterns = [
        r"C[UÜVLI1|]{1,3}T[:\s\-—]*([0-9]{2}-?[0-9]{8}-?[0-9])",
        r"C[UÜVLI1|]{1,3}T[:\s\-—]*([0-9]{11})",
        r"\b([0-9]{2}-?[0-9]{8}-?[0-9])\b",
        r"\b([0-9]{11})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if _valid_cuit_digits(digits):
                return digits
    for raw in re.findall(r"\b\d{11}\b", text or ""):
        if _valid_cuit_digits(raw):
            return raw
    return ""


def _extract_invoice_number(text: str) -> str:
    patterns = [
        r"Punto de Venta[:\s]*([0-9]{1,5}).{0,40}?Comp\.?\s*Nro\.?[:\s]*([0-9]{1,8})",
        r"Pto\.?\s*Vta\.?[:\s]*([0-9]{1,5}).{0,40}?Comp\.?\s*Nro\.?[:\s]*([0-9]{1,8})",
        r"Nro\.?[:\s]*([0-9]{1,5})[-\s]*([0-9]{1,8})",
        r"Comprobante[:\s]*([0-9]{1,5})[-\s]*([0-9]{1,8})",
        r"\b([0-9]{4,5})\s*[-–—]\s*([0-9]{6,8})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I | re.S)
        if m:
            if m.lastindex and m.lastindex >= 2:
                return f"{m.group(1).zfill(5)}-{m.group(2).zfill(8)}"
    m = re.search(r"\b([0-9]{4,5}-[0-9]{6,8})\b", text)
    if m:
        left, right = m.group(1).split("-")
        return f"{left.zfill(5)}-{right.zfill(8)}"
    return ""


def _extract_invoice_date(lines: list[str]) -> str:
    label_patterns = [
        r"fecha de emisión",
        r"fecha de emision",
        r"fecha de factura",
        r"f\.?\s*emisi[oó]n",
        r"fecha\s*:\s*",
    ]
    skip_markers = ("vto", "venc", "cae", "per[ií]odo", "facturado desde", "hasta", "inicio")

    for pattern in label_patterns:
        for idx, line in enumerate(lines):
            if not re.search(pattern, line, flags=re.I):
                continue
            lower = line.lower()
            if any(marker in lower for marker in skip_markers):
                continue

            direct = _parse_date_token(line)
            if direct:
                return direct

            # Look a little further ahead than before; OCR often places the date
            # on the next line or two after the label.
            for look_ahead in range(1, 7):
                if idx + look_ahead < len(lines):
                    candidate_line = lines[idx + look_ahead]
                    candidate = _parse_date_token(candidate_line)
                    if candidate:
                        return candidate

    # Fallback: first date token anywhere in the document.
    for line in lines:
        token = _parse_date_token(line)
        if token:
            return token
    return ""


def _amount_candidates(line: str) -> list[Decimal]:
    tokens = re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", line)
    out: list[Decimal] = []
    for token in tokens:
        value = _dec(token)
        if value is not None:
            out.append(value)
    # Prefer the last amount-like token on the line; this handles cases such as
    # "IVA 21% 611098.49" where the first number is a rate, not an amount.
    return out[-1:] if out else []


def _amount_token_candidates(line: str) -> list[tuple[str, Decimal]]:
    out: list[tuple[str, Decimal]] = []
    for token in re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", line):
        value = _dec(token)
        if value is not None:
            out.append((token, value))
    return out[-1:] if out else []


def _find_amount_after_labels(
    lines: list[str],
    labels: list[str],
    *,
    max_back: int = 4,
    max_forward: int = 5,
) -> Decimal | None:
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        normalized = line.lower()
        if any(re.search(pattern, normalized, flags=re.I) for pattern in labels):
            window: list[tuple[int, int, Decimal]] = []
            for offset in range(-max_back, max_forward + 1):
                pos = idx + offset
                if 0 <= pos < len(lines):
                    for candidate in _amount_candidates(lines[pos]):
                        window.append((abs(offset), offset, candidate))
            if window:
                # Prefer closer matches; on ties, prefer the same line / forward
                # context over backward context.
                window.sort(key=lambda item: (item[0], 0 if item[1] >= 0 else 1))
                return window[0][2]
    return None


def _extract_amount_in_following_lines(lines: list[str], idx: int, *, max_forward: int = 2) -> Decimal | None:
    for offset in range(1, max_forward + 1):
        pos = idx + offset
        if not (0 <= pos < len(lines)):
            continue
        candidates = _amount_candidates(lines[pos])
        if candidates:
            return candidates[-1]

    candidates = _amount_candidates(lines[idx])
    return candidates[-1] if candidates else None


def _extract_iva_summary(lines: list[str]) -> Decimal | None:
    total_idx = None
    start_idx = 0

    for idx in range(len(lines) - 1, -1, -1):
        normalized = _normalize_label_text(lines[idx])
        if total_idx is None and re.search(r"^IMPORTE TOTAL\b|^TOTAL A PAGAR\b|^IMPORTE A PAGAR\b", normalized):
            total_idx = idx
            continue
        if total_idx is not None and re.search(r"^IMPORTE NETO GRAVADO\b|^SUBTOTAL\s*:\s*$", normalized):
            start_idx = idx
            break

    block = lines[start_idx : (total_idx + 1 if total_idx is not None else len(lines))]
    amounts: list[Decimal] = []

    for idx, line in enumerate(block):
        normalized = _normalize_label_text(line)
        if not re.search(r"\bI\W*V\W*A\W*\d+(?:[.,]\d+)?\s*%?", normalized):
            continue
        amount = None
        for pos in range(idx, min(len(block), idx + 5)):
            if pos > idx and re.search(r"\b(?:VTO|VIO|CAE|TOTAL|SUBTOTAL|PERC)\b", _normalize_label_text(block[pos])):
                break
            candidates = _amount_candidates(block[pos])
            candidates = [candidate for candidate in candidates if candidate > Decimal("100")]
            if candidates:
                amount = candidates[-1]
                break
        if amount is None:
            continue
        amounts.append(amount)

    if not amounts:
        return None

    return sum(amounts, Decimal("0"))


def _extract_jurisdiction(text: str) -> str:
    normalized = _normalize_label_text(text)
    compact = re.sub(r"[^A-Z0-9]+", " ", normalized)
    for alias, province in JURISDICTION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", compact):
            if province == "CABA":
                return "CABA"
            return province.title()
    for province in sorted(PROVINCES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(province)}\b", compact):
            if province == "CIUDAD AUTONOMA DE BUENOS AIRES":
                return "CABA"
            return province.title()
    return ""


def _codjur_for_jurisdiction(jurisdiction: str) -> str:
    normalized = _normalize_label_text(jurisdiction)
    if normalized in JURISDICTION_ALIASES:
        normalized = JURISDICTION_ALIASES[normalized]
    return JURISDICTION_CODJUR.get(normalized, "")


def _is_fragment_of_larger_amount(amount: Decimal, larger: Decimal) -> bool:
    if amount <= 0 or larger <= amount:
        return False
    amount_cents = int((amount * 100).to_integral_value())
    larger_cents = int((larger * 100).to_integral_value())
    return larger_cents % 100 == amount_cents % 100 and str(larger_cents).endswith(str(amount_cents))


def _dedupe_iibb_perception_fragments(perceptions: list[IIBBPerception]) -> list[IIBBPerception]:
    filtered: list[IIBBPerception] = []
    for item in sorted(perceptions, key=lambda p: p.amount or Decimal("0"), reverse=True):
        amount = item.amount or Decimal("0")
        same_jurisdiction_larger = [
            existing.amount or Decimal("0")
            for existing in filtered
            if (existing.jurisdiction or "").upper() == (item.jurisdiction or "").upper()
            and (existing.codjur or "") == (item.codjur or "")
        ]
        if amount <= Decimal("50") and same_jurisdiction_larger:
            continue
        if any(_is_fragment_of_larger_amount(amount, larger) for larger in same_jurisdiction_larger):
            continue
        filtered.append(item)
    return sorted(filtered, key=lambda p: perceptions.index(p))


def _extract_iibb_amount_near(lines: list[str], idx: int) -> Decimal | None:
    window: list[tuple[int, str, Decimal, str]] = []
    stop_markers = ["SUBTOTAL", "TOTAL", "IVA", "CAE", "VTO", "COMP. NRO", "PUNTO DE VENTA"]

    for offset in range(0, 4):
        pos = idx + offset
        if not (0 <= pos < len(lines)):
            continue
        line = lines[pos]
        upper = _normalize_label_text(line)
        if offset > 0 and any(marker in upper for marker in stop_markers):
            if window:
                break
            continue
        candidates = _amount_token_candidates(line)
        if not candidates:
            continue
        token, value = candidates[-1]
        if any(sep in token for sep in [",", "."]) or "$" in line:
            window.append((offset, token, value, line))

    if not window:
        return None

    if len(window) >= 2:
        first = window[0]
        later = [item for item in window[1:] if item[2] > first[2]]
        if first[2] <= Decimal("50") and later:
            return later[-1][2]

    return window[0][2]


def _extract_iibb_perceptions(lines: list[str]) -> list[IIBBPerception]:
    perceptions: list[IIBBPerception] = []
    seen: set[tuple[str, str]] = set()
    label_patterns = [
        r"\bib\.?\s*(?:mision|misiones|caba|arba|buenos\s+aires|catamarca|chaco|chubut|cordoba|corrientes|entre\s+rios|formosa|jujuy|la\s+pampa|la\s+rioja|mendoza|neuquen|rio\s+negro|salta|san\s+juan|san\s+luis|santa\s+cruz|santa\s+fe|santiago\s+del\s+estero|tierra\s+del\s+fuego|tucuman)",
        r"percepciones?\s*iibb",
        r"percepciones?\s+ingresos\s+brutos",
        r"ingresos\s+brutos",
        r"iibb",
    ]

    for idx, line in enumerate(lines):
        normalized = _normalize_label_text(line)
        window_text = " ".join(lines[idx : min(len(lines), idx + 3)])
        jurisdiction = _extract_jurisdiction(window_text)
        bare_perception_with_jurisdiction = bool(
            re.fullmatch(r"PERCEPCIONES?", normalized)
            and jurisdiction
        )
        if not bare_perception_with_jurisdiction and not any(re.search(pattern, normalized, flags=re.I) for pattern in label_patterns):
            continue
        if any(skip in normalized for skip in ["FECHA DE INICIO", "CONDICION", "RESPONSABLE", "PADRON"]):
            continue

        amount = _extract_iibb_amount_near(lines, idx)
        if amount is None:
            continue

        key = (jurisdiction.upper(), format(amount, "f"))
        if key in seen:
            continue
        seen.add(key)
        perceptions.append(IIBBPerception(jurisdiction=jurisdiction, codjur=_codjur_for_jurisdiction(jurisdiction), amount=amount))

    return _dedupe_iibb_perception_fragments(perceptions)


def _extract_totals(lines: list[str]) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, list[IIBBPerception]]:
    subtotal = _find_amount_after_labels(lines, [r"importe neto gravado"], max_back=0, max_forward=3)
    if subtotal is None:
        subtotal = _find_amount_after_labels(lines, [r"\bsubtotal\b"])

    iva = _extract_iva_summary(lines)
    if iva is None:
        iva = _find_amount_after_labels(lines, [r"\biva\b(?:\s*contenido)?\s*:", r"\biva\b"])
    if iva is None:
        iva = _find_amount_after_labels(lines, [r"impuesto al valor agregado", r"impuesto interno"])
    perception_detail = _extract_iibb_perceptions(lines)
    if perception_detail:
        perceptions = sum((item.amount or Decimal("0")) for item in perception_detail)
    else:
        perceptions = _find_amount_after_labels(
            lines,
            [r"percepciones\s*iibb", r"percepcion\s*iibb", r"percepciones\s+ingresos\s+brutos", r"ingresos brutos"],
            max_back=0,
            max_forward=2,
        )
    total = _find_amount_after_labels(lines, [r"\bimporte total\s*:", r"\btotal\s*:", r"total a pagar", r"importe a pagar"])
    return subtotal, iva, perceptions, total, perception_detail


def _extract_currency(text: str) -> str:
    upper = text.upper()
    if re.search(r"\b(U\$S|USD|DOLAR(?:ES)?)\b", upper):
        return "USD"
    if re.search(r"\b(EUR|EUROS?)\b", upper):
        return "EUR"
    if re.search(r"\bARS\b", upper):
        return "ARS"
    return "ARS"


def _is_numeric_line(line: str) -> bool:
    return bool(re.fullmatch(r"[$\s0-9.,-]+", line.strip())) and any(ch.isdigit() for ch in line)


def _extract_line_items(lines: list[str]) -> list[LineItem]:
    start_idx = None
    for idx, line in enumerate(lines):
        if re.search(r"producto\s*/\s*servicio|detalle|descripcion|descripción", line, flags=re.I):
            start_idx = idx + 1
            break
    if start_idx is None:
        # Fallback for heavily OCR-corrupted documents where the detail header
        # is missing but the item line still contains multiple amounts.
        for line in lines:
            upper = line.upper()
            if any(word in upper for word in ["FACTURA", "CAE", "TOTAL", "SUBTOTAL", "IVA", "VTO", "VENCE", "DOMICILIO"]):
                continue
            tokens = re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", line)
            numbers = [_dec(tok) for tok in tokens]
            numbers = [n for n in numbers if n is not None]
            if len(numbers) > 3:
                filtered = [n for n in numbers if not (n == n.to_integral_value() and abs(int(n)) < 100)]
                if len(filtered) >= 3:
                    numbers = filtered
            has_decimal = any(re.search(r"[.,]\d+$", tok) for tok in tokens)
            if len(numbers) >= 3 and has_decimal and sum(ch.isalpha() for ch in line) >= 8:
                desc = re.sub(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", " ", line)
                desc = re.sub(r"\s{2,}", " ", desc).strip(" -–—\t")
                item = LineItem(description=desc)
                item.quantity = numbers[0]
                item.unit_price = numbers[1]
                item.subtotal = numbers[-1]
                return [item]
        return []

    def _is_footer_line(line: str) -> bool:
        return bool(
            re.search(
                r"otros\s+tributos|percepciones?\s*$|percepci[oó]n\s+(?:i\.?\s*v\.?\s*a\.?|de\s+ingresos\s+brutos)|importe\s+otros\s+tributos|importe\s+neto\s+gravado|importe\s+total\s*:|subtotal\s*:|cae\b|vto\.\s*de\s*cae|total:\s*",
                line,
                flags=re.I,
            )
        )

    def _is_header_noise(line: str) -> bool:
        return bool(
            re.search(
                r"^\s*(cantidad|u\.?\s*medida|precio\s*unit|subtotal(?:\s*c/?\s*iva)?|\%?\s*bonif|imp\.?\s*bonif|c[oó]digo|producto\s*/\s*servicio|al[ií]cuota|iva)\b",
                line,
                flags=re.I,
            )
        )

    def _is_unit_line(line: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúñÑ. ]{2,}", line.strip())) and bool(
            re.search(r"unidad|unidades|uni\b|kg\b|kgs\b|lt\b|lts\b|litro|litros|caja|cajas|bolsa|bolsas", line, flags=re.I)
        )

    def _is_percent_line(line: str) -> bool:
        return bool(re.fullmatch(r"\d+(?:[.,]\d+)?\s*%", line.strip()))

    def _is_value_like(line: str) -> bool:
        return _is_numeric_line(line) or _is_unit_line(line) or _is_percent_line(line)

    end_idx = len(lines)
    for idx in range(start_idx, len(lines)):
        # Stop only at the footer totals area, not at the "Subtotal" column header.
        if _is_footer_line(lines[idx]):
            end_idx = idx
            break

    block = lines[start_idx:end_idx]
    if not block:
        return []

    horizontal_items = _extract_horizontal_line_items(block)
    if horizontal_items:
        return horizontal_items

    has_subtotal_with_tax = any(re.search(r"subtotal\s*c/?\s*iva", line, flags=re.I) for line in block)
    content = [line for line in block if line.strip() and not _is_header_noise(line)]
    if not content:
        return []

    def _build_item(description_lines: list[str], value_lines: list[str]) -> LineItem | None:
        description = " ".join(part.strip() for part in description_lines if part.strip()).strip()
        numbers = []
        for line in value_lines:
            if _is_numeric_line(line):
                amount = _dec(line)
                if amount is not None:
                    numbers.append(amount)
        if not description and not numbers:
            return None

        item = LineItem(description=description)
        start_num_idx = 0
        # Some invoices include a year inside the description block (e.g. 2026.)
        # before the actual quantity. If the first extracted number looks like a
        # year and there is another number immediately after it, skip it.
        if len(numbers) >= 2 and numbers[0] == numbers[0].to_integral_value():
            first_int = int(numbers[0])
            if 1900 <= first_int <= 2100:
                start_num_idx = 1
        if start_num_idx < len(numbers):
            item.quantity = numbers[start_num_idx]
        if len(numbers) >= start_num_idx + 2:
            item.unit_price = numbers[start_num_idx + 1]
        if has_subtotal_with_tax and len(numbers) >= start_num_idx + 4:
            item.subtotal = numbers[-2]
        elif len(numbers) >= start_num_idx + 3:
            item.subtotal = numbers[-1]
        elif len(numbers) >= start_num_idx + 2:
            item.subtotal = numbers[start_num_idx + 1]
        return item

    items: list[LineItem] = []
    description_lines: list[str] = []
    value_lines: list[str] = []
    in_value_section = False

    for line in content:
        if _is_footer_line(line):
            break
        if not in_value_section:
            if _is_value_like(line):
                value_lines.append(line)
                in_value_section = True
            else:
                description_lines.append(line)
            continue

        if _is_value_like(line):
            value_lines.append(line)
            continue

        item = _build_item(description_lines, value_lines)
        if item is not None:
            items.append(item)
        description_lines = [line]
        value_lines = []
        in_value_section = False

    item = _build_item(description_lines, value_lines)
    if item is not None:
        items.append(item)

    return [item for item in items if item.description or item.subtotal is not None or item.unit_price is not None]


def _extract_horizontal_line_items(block: list[str]) -> list[LineItem]:
    content = [
        line.strip()
        for line in block
        if line.strip()
        and not re.search(
            r"^\s*(art[ií]culo|codigo|c[oó]digo|unid\.?|descripci[oó]n|cantidad|bonif\.?|unitario|importe)\b",
            line,
            flags=re.I,
        )
    ]
    if not content:
        return []

    vertical_items = _extract_vertical_ocr_items(content)
    if vertical_items:
        return vertical_items

    items: list[LineItem] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if not current:
            return
        item = LineItem(description=re.sub(r"\s+", " ", " ".join(current["description_parts"])).strip())
        item.quantity = current.get("quantity")
        item.unit_price = current.get("unit_price")
        item.subtotal = current.get("subtotal")
        if item.description or item.quantity is not None or item.unit_price is not None or item.subtotal is not None:
            items.append(item)
        current = None

    for line in content:
        compact = re.match(
            r"^\s*(?P<qty>\d+(?:[.,]\d{2,4}))\s+(?P<body>.*?[A-Za-zÁÉÍÓÚáéíóúñÑ].*?)\s+(?P<unit>\d+(?:[.,]\d{2,4}))\s+(?P<subtotal>\d{4,}(?:[.,]\d{1,2})?)\s*$",
            line,
        )
        if compact and not re.search(r"TE\.?C|FACTURA\s+ANTICIPADA", compact.group("body"), flags=re.I):
            finish()
            item = LineItem(description=re.sub(r"\s+", " ", compact.group("body")).strip())
            item.quantity = _dec_quantity(compact.group("qty"))
            item.unit_price = _dec(compact.group("unit"))
            item.subtotal = _dec_ocr_money(compact.group("subtotal"))
            items.append(item)
            current = None
            continue

        start = re.match(
            r"^\s*(?P<article>\d+)\s+(?P<unit>[A-Za-zÁÉÍÓÚáéíóúñÑ.]+)\s+(?P<body>.+?)\s+(?P<qty>\d+(?:[.,]\d{1,4}))\s*$",
            line,
        )
        if start:
            finish()
            current = {
                "description_parts": [start.group("body").strip()],
                "quantity": _dec_quantity(start.group("qty")),
                "unit_price": None,
                "subtotal": None,
            }
            continue

        trailing_amounts = re.findall(r"[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?", line)
        if current and len(trailing_amounts) >= 2:
            prefix = re.sub(
                r"(?:[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?\s*){2,}$",
                "",
                line,
            ).strip()
            if prefix:
                current["description_parts"].append(prefix)
            amounts = [_dec(token) for token in trailing_amounts]
            amounts = [amount for amount in amounts if amount is not None]
            if len(amounts) >= 2:
                current["unit_price"] = amounts[-2]
                current["subtotal"] = amounts[-1]
            continue

        if current:
            current["description_parts"].append(line)

    finish()
    return items


def _extract_vertical_ocr_items(content: list[str]) -> list[LineItem]:
    items: list[LineItem] = []
    consumed: set[int] = set()
    for idx in range(0, max(0, len(content) - 3)):
        if idx in consumed:
            continue
        quantity = _dec_quantity(content[idx])
        if quantity is None or quantity <= 0:
            continue
        description = content[idx + 1].strip()
        if not description or sum(ch.isalpha() for ch in description) < 8:
            continue
        if re.search(r"TE\.?C|FACTURA\s+ANTICIPADA|PARA TODOS|TOTAL|SUBTOTAL|CAE|VTO", description, flags=re.I):
            continue
        unit_price = _dec_ocr_money(content[idx + 2])
        subtotal = _dec_ocr_money(content[idx + 3])
        if unit_price is None or subtotal is None:
            continue
        if unit_price <= 0 or subtotal <= 0:
            continue
        item = LineItem(description=re.sub(r"\s+", " ", description).strip())
        item.quantity = quantity
        item.unit_price = unit_price
        item.subtotal = subtotal
        items.append(item)
        consumed.update({idx, idx + 1, idx + 2, idx + 3})

        cursor = idx + 4
        while cursor + 1 < len(content):
            if cursor in consumed:
                cursor += 1
                continue
            detail_qty = _dec_quantity(content[cursor])
            detail_desc = content[cursor + 1].strip()
            if detail_qty is None or detail_qty <= 0:
                break
            if not detail_desc or sum(ch.isalpha() for ch in detail_desc) < 4:
                break
            if re.search(r"PARA TODOS|TOTAL|SUBTOTAL|CAE|VTO|RECARGO|DESCUENTO|OTROS\s+IMP|IVA", detail_desc, flags=re.I):
                break
            detail = LineItem(description=re.sub(r"\s+", " ", detail_desc).strip())
            detail.quantity = detail_qty
            items.append(detail)
            consumed.update({cursor, cursor + 1})
            cursor += 2

    return items


def _dec_quantity(value: str) -> Decimal | None:
    value = (value or "").strip().replace(" ", "")
    if not value:
        return None
    if re.fullmatch(r"\d+,\d{1,4}", value):
        value = value.replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation:
        return _dec(value)


def _dec_ocr_money(value: str) -> Decimal | None:
    raw = (value or "").strip().replace(" ", "")
    if re.fullmatch(r"\d{6,}", raw):
        try:
            return Decimal(raw) / Decimal("100")
        except InvalidOperation:
            return None
    return _dec(raw)


def extract_invoice_data(
    raw_text: str,
    *,
    source_file: str = "",
    provider_profile: dict[str, Any] | None = None,
    provider_key: str = "",
) -> InvoiceData:
    text = normalize_text(raw_text)
    lines = _lines(text)
    profile = _normalized_profile(provider_profile)
    document_type = looks_like_credit_or_debit(text)
    provider_name = _extract_provider_name(lines, profile, provider_key)
    cuit = _extract_cuit(text, profile)
    invoice_number = _extract_invoice_number(text)
    invoice_date = _extract_invoice_date(lines)
    if not invoice_date and source_file:
        m = re.search(r"(\d{2})[._-](\d{2})[._-](\d{4})", Path(source_file).name)
        if m:
            invoice_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    currency = _extract_currency(text)
    subtotal, iva, perceptions_iibb, total, perceptions_iibb_detail = _extract_totals(lines)
    line_items = _extract_line_items(lines)

    fields_found = sum(
        1
        for value in [provider_name, cuit, invoice_number, invoice_date, subtotal, iva, total]
        if value not in (None, "")
    )
    confidence = min(0.35 + (fields_found * 0.08) + (0.05 if line_items else 0.0), 0.95)
    notes = "Extracción local heurística"
    if not line_items:
        notes += "; sin detalle de ítems detectado"

    invoice = InvoiceData(
        document_type=document_type,
        provider_name=provider_name,
        cuit=cuit,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        currency=currency,
        subtotal=subtotal,
        iva=iva,
        perceptions_iibb=perceptions_iibb,
        perceptions_iibb_detail=perceptions_iibb_detail,
        total=total,
        line_items=line_items,
        confidence=confidence,
        notes=notes,
        source_file=source_file,
        raw_text=text,
        provider_profile=profile,
    )
    return invoice
