"""Extract title-block fields from engineering drawings (Tata / IS / OEM patterns)."""

from __future__ import annotations

import math
import re
from pathlib import Path

import fitz

_META_KEYS = (
    "part_number",
    "part_name",
    "revision",
    "material",
    "mass",
    "finish",
    "serial_number",
    "report_date",
    "company_name",
    "supplier_name",
    "customer_name",
    "authorized_signatory",
)

_LABEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "part_number",
        re.compile(
            r"(?i)^(?:PART\s*(?:NO\.?|NUMBER|#)|P\s*/\s*N|P/?N|ITEM\s*(?:NO\.?|NUMBER)|"
            r"DRAWING\s*(?:NO\.?|NUMBER)|DWG\.?\s*(?:NO\.?|NUMBER)?|DRG\.?\s*(?:NO\.?|NUMBER)?|"
            r"DRN\.?\s*(?:NO\.?)?|IDENT(?:IFICATION)?\s*(?:NO\.?|NUMBER)?|"
            r"ARTICLE\s*(?:NO\.?|NUMBER)|DOC(?:UMENT)?\s*(?:NO\.?|NUMBER))\s*:?$"
        ),
    ),
    ("revision", re.compile(r"(?i)^REV(?:ISION)?\.?\s*(?:NO\.?)?\s*:?$")),
    (
        "part_name",
        re.compile(
            r"(?i)^(?:PART\s*NAME|DESCRIPTION|TITLE|NOMENCLATURE|NAME\s*OF\s*PART|"
            r"COMPONENT\s*NAME|ITEM\s*NAME|ASSEMBLY\s*NAME)\s*:?$"
        ),
    ),
    (
        "material",
        re.compile(r"(?i)^(?:MATERIAL|MATL\.?|MAT(?:ERIA)?L\.?|SPEC(?:IFICATION)?|MAT(?:L)?\.?)\s*:?$"),
    ),
    ("mass", re.compile(r"(?i)^(?:MASS|WEIGHT|WT\.?|NET\s*WEIGHT|MASS\s*\(?KG\)?)\s*:?$")),
    (
        "finish",
        re.compile(
            r"(?i)^(?:FINISH(?:\s*TREATMENT)?|SURFACE\s*FINISH|TREATMENT|"
            r"SURFACE\s*TREATMENT|COATING|PAINT|HEAT\s*TREATMENT)\s*:?$"
        ),
    ),
    (
        "serial_number",
        re.compile(
            r"(?i)^(?:SERIAL\s*(?:NO\.?|NUMBER)|S\s*/\s*N|S/?N|SR\.?\s*NO\.?|"
            r"SL\.?\s*NO\.?|SI\.?\s*NO\.?|SER\.?\s*NO\.?)\s*:?$"
        ),
    ),
    (
        "report_date",
        re.compile(r"(?i)^(?:DATE|DT\.?|ISSUE\s*DATE|DWG\.?\s*DATE|DRAWING\s*DATE)\s*:?$"),
    ),
    (
        "company_name",
        re.compile(
            r"(?i)^(?:COMPANY(?:\s*NAME)?|ORGANIZATION|ORG\.?|OEM|"
            r"MANUFACTURER|MFR\.?|WORKS|PLANT)\s*:?$"
        ),
    ),
    (
        "supplier_name",
        re.compile(r"(?i)^(?:SUPPLIER(?:\s*NAME)?|VENDOR(?:\s*NAME)?|SUB[\s-]?CONTRACTOR|SUBCON)\s*:?$"),
    ),
    (
        "customer_name",
        re.compile(r"(?i)^(?:CUSTOMER(?:\s*NAME)?|CLIENT(?:\s*NAME)?|BUYER|PURCHASER|END\s*USER)\s*:?$"),
    ),
    (
        "authorized_signatory",
        re.compile(r"(?i)^(?:AUTHORIZED\s*SIGNATORY|AUTHORISED\s*SIGNATORY|APPROVED\s*BY|APPROVAL)\s*:?$"),
    ),
]

_COMBINED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "part_number",
        re.compile(
            r"(?i)^(?:PART\s*(?:NO\.?|NUMBER|#)|P\s*/\s*N|P/?N|"
            r"DRAWING\s*(?:NO\.?|NUMBER)|DWG\.?\s*(?:NO\.?|NUMBER)?|DRG\.?\s*(?:NO\.?|NUMBER)?|"
            r"ITEM\s*(?:NO\.?|NUMBER)|IDENT(?:IFICATION)?\s*(?:NO\.?)?)\s*:?\s*(.+)$"
        ),
    ),
    ("revision", re.compile(r"(?i)^REV(?:ISION)?\.?\s*:?\s*(.+)$")),
    (
        "part_name",
        re.compile(
            r"(?i)^(?:PART\s*NAME|DESCRIPTION|TITLE|NOMENCLATURE|ITEM\s*NAME)\s*:?\s*(.+)$"
        ),
    ),
    ("material", re.compile(r"(?i)^(?:MATERIAL|MATL\.?|MAT(?:ERIA)?L\.?)\s*:?\s*(.+)$")),
    ("mass", re.compile(r"(?i)^(?:MASS|WEIGHT|WT\.?)\s*:?\s*(.+)$")),
    (
        "finish",
        re.compile(
            r"(?i)^(?:FINISH(?:\s*TREATMENT)?|SURFACE\s*FINISH|TREATMENT|COATING|PAINT)\s*:?\s*(.+)$"
        ),
    ),
    (
        "serial_number",
        re.compile(
            r"(?i)^(?:SERIAL\s*(?:NO\.?|NUMBER)|S\s*/\s*N|S/?N|SR\.?\s*NO\.?|SI\.?\s*NO\.?)\s*:?\s*(.+)$"
        ),
    ),
    (
        "report_date",
        re.compile(r"(?i)^(?:DATE|DT\.?|ISSUE\s*DATE|DWG\.?\s*DATE)\s*:?\s*(.+)$"),
    ),
    (
        "company_name",
        re.compile(
            r"(?i)^(?:COMPANY(?:\s*NAME)?|ORGANIZATION|MANUFACTURER)\s*:?\s*(.+)$"
        ),
    ),
    ("supplier_name", re.compile(r"(?i)^(?:SUPPLIER(?:\s*NAME)?|VENDOR|SUB[\s-]?CONTRACTOR)\s*:?\s*(.+)$")),
    ("customer_name", re.compile(r"(?i)^(?:CUSTOMER(?:\s*NAME)?|CLIENT|BUYER|PURCHASER)\s*:?\s*(.+)$")),
    (
        "authorized_signatory",
        re.compile(r"(?i)^(?:AUTHORIZED\s*SIGNATORY|AUTHORISED\s*SIGNATORY|APPROVED\s*BY)\s*:?\s*(.+)$"),
    ),
]

_NOISE = re.compile(
    r"(?i)^(sheet|scale|drawn|checked|approved|date|dwg|drg|format|size|of\s+\d+|"
    r"rev\.?\s*by|designed|project|zone|iso|a\d|a0|a1|notes?|engraving|"
    r"unless\s+otherwise|refer|see\s+note|all\s+dim|:|-|—|–|"
    r"error:?\s*no\s*reference|no\s*reference|n/?a|nil|none|tbd|xxx)$"
)

_BAD_VALUE = re.compile(
    r"(?i)^(notes?:?-?|engraving\s*notes?:?-?|in\s*microns|finish\s*in\s*microns|"
    r"error:?\s*no\s*reference|no\s*reference|:|-|—|–|"
    r"unless\s+otherwise.*|refer\s+sheet.*|n/?a|nil|none|tbd)$"
)

# Company names often appear as logos / stamps without a label
_COMPANY_HINTS = re.compile(
    r"(?i)\b("
    r"TATA\s+ADVANCED\s+SYSTEMS(?:\s+LIMITED)?|"
    r"TATA\s+MOTORS(?:\s+LIMITED)?|"
    r"[A-Z][A-Z0-9 &.\-]{4,}(?:\s+LIMITED|\s+LTD\.?|\s+PVT\.?\s*LTD\.?)"
    r")\b"
)


def _clean(text: str) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip()


def _valid_value(key: str, val: str) -> bool:
    v = _clean(val)
    if not v or len(v) < 1:
        return False
    if _NOISE.match(v) or _BAD_VALUE.match(v):
        return False
    if v in {":", "-", "—", "–"}:
        return False
    if re.search(r"(?i)error|no\s*reference", v):
        return False
    if key == "part_number":
        if len(v) > 64:
            return False
        if re.search(r"(?i)note|engraving|unless|otherwise|micron|finish|uncoated|coated", v):
            return False
        # Drawing / part codes: digit-heavy or OEM alphanumerics
        if not (
            re.search(r"\d{6,}", v)
            or re.match(r"^[A-Z]{2,}\d{3,}[A-Z0-9./-]*$", v, re.I)
            or re.match(r"^[A-Z0-9]+[-./][A-Z0-9./-]{4,}$", v, re.I)
        ):
            return False
    if key == "revision":
        return bool(re.match(r"^[A-Za-z0-9.\-]{1,12}$", v))
    if key == "part_name":
        if len(v) < 3 or v in {":", "-"}:
            return False
        if re.search(
            r"(?i)^(?:finish|engraving|in\s*microns|material|mass|drawing|"
            r"uncoated|coated|painted|plated|galvaniz|anodiz|rel)$",
            v,
        ):
            return False
    if key == "report_date":
        return bool(re.search(r"\d{1,4}", v)) and len(v) <= 24
    if key == "authorized_signatory":
        return len(v) >= 2 and not re.match(r"(?i)^(sign|signature|date|approved)$", v)
    if key == "material":
        if len(v) < 3 or re.match(r"(?i)^(name|mat|spec|appd|sht|xxx+|material:?)$", v):
            return False
    if key == "finish":
        if re.match(r"(?i)^(?:in\s*microns|finish\s*in\s*microns|appd|xxx+|material:?|finish:?)$", v):
            return False
        if len(v) < 2:
            return False
    if key in {"material", "finish"} and re.search(r"(?i)error|no\s*reference", v):
        return False
    if key in {"company_name", "supplier_name", "customer_name"}:
        if len(v) < 3:
            return False
        if re.match(r"(?i)^(supplier|customer|company|vendor|systems\s+limited)$", v):
            return False
    return True


def _in_title_zone(cx: float, cy: float, page_rect: fitz.Rect) -> bool:
    w, h = page_rect.width, page_rect.height
    # ISO / IS / Tata title blocks: bottom band (full width), right strip, top-right rev
    bottom = cy > page_rect.y0 + h * 0.58
    right = cx > page_rect.x0 + w * 0.48 and cy > page_rect.y0 + h * 0.38
    top_right = cy < page_rect.y0 + h * 0.28 and cx > page_rect.x0 + w * 0.48
    left_bottom = cx < page_rect.x0 + w * 0.45 and cy > page_rect.y0 + h * 0.70
    return bottom or right or top_right or left_bottom


def _page_title_items(page: fitz.Page) -> list[tuple[str, fitz.Rect, float]]:
    page_rect = page.rect
    items: list[tuple[str, fitz.Rect, float]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            line_text = _clean(" ".join(s.get("text", "") for s in spans))
            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            size = float(spans[0].get("size") or 8)
            if line_text and _in_title_zone(cx, cy, page_rect):
                items.append((line_text, fitz.Rect(x0, y0, x1, y1), size))
            for span in spans:
                t = _clean(span.get("text", ""))
                if not t or t == line_text:
                    continue
                sx0, sy0, sx1, sy1 = span["bbox"]
                scx, scy = (sx0 + sx1) / 2, (sy0 + sy1) / 2
                if _in_title_zone(scx, scy, page_rect):
                    items.append((t, fitz.Rect(sx0, sy0, sx1, sy1), float(span.get("size") or 8)))
    return items


def _find_nearby_value(
    items: list[tuple[str, fitz.Rect, float]],
    label_rect: fitz.Rect,
    skip_idx: int,
    key: str,
) -> str:
    lcx = (label_rect.x0 + label_rect.x1) / 2
    lcy = (label_rect.y0 + label_rect.y1) / 2
    best: str | None = None
    best_score = 1e9
    reach = 180.0
    for j, (t, r, _sz) in enumerate(items):
        if j == skip_idx:
            continue
        if _NOISE.match(t) or len(t) < 1:
            continue
        if not _valid_value(key, t):
            continue
        if any(pat.match(t) for _, pat in _LABEL_PATTERNS):
            continue
        vcx = (r.x0 + r.x1) / 2
        vcy = (r.y0 + r.y1) / 2
        dist = math.hypot(vcx - lcx, vcy - lcy)
        if dist > reach:
            continue
        right_bonus = 0.0 if vcx >= label_rect.x0 - 4 else 35.0
        below_bonus = 0.0 if vcy >= label_rect.y0 - 4 else 20.0
        score = dist + right_bonus + below_bonus - min(len(t), 24) * 0.4
        if score < best_score:
            best_score = score
            best = t
    return best or ""


def _guess_company(items: list[tuple[str, fitz.Rect, float]], full_text: str = "") -> str:
    # Prefer well-known OEM stamps first
    blob = "\n".join([t for t, _r, _s in items] + [full_text or ""])
    m = re.search(r"(?i)\b(TATA\s+ADVANCED\s+SYSTEMS(?:\s+LIMITED)?)\b", blob)
    if m:
        return _clean(m.group(1))
    best = ""
    for text, _r, _sz in items:
        m = _COMPANY_HINTS.search(text)
        if m:
            name = _clean(m.group(1))
            if _valid_value("company_name", name) and len(name) > len(best):
                best = name
    if best:
        return best
    m = _COMPANY_HINTS.search(full_text or "")
    if m:
        name = _clean(m.group(1))
        if _valid_value("company_name", name):
            return name
    return ""


def _line_fallback(full_text: str, found: dict[str, str]) -> dict[str, str]:
    """Second pass over plain text lines for OEM title-block patterns."""
    lines = [_clean(x) for x in (full_text or "").splitlines() if _clean(x)]
    out = dict(found)
    for i, line in enumerate(lines):
        # DRAWING NO. / PART NO. on same or next line
        if not out.get("part_number"):
            m = re.match(
                r"(?i)^(?:DRAWING|DWG|DRG|PART)\s*(?:NO\.?|NUMBER)?\s*:?\s*([A-Z0-9][-A-Z0-9./]{5,})$",
                line,
            )
            if m and _valid_value("part_number", m.group(1)):
                out["part_number"] = m.group(1)
            elif re.match(r"(?i)^(?:DRAWING|DWG|DRG|PART)\s*(?:NO\.?|NUMBER)?\s*:?$", line):
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if _valid_value("part_number", nxt):
                    out["part_number"] = nxt
        if not out.get("part_name"):
            m = re.search(
                r"(?i)((?:CHASSIS[_ ]?)?STRUCTURE\s+WELDED\s+ASSEMBLY|[A-Z][A-Z0-9 _\-]{8,}ASSEMBLY)",
                line,
            )
            if m and _valid_value("part_name", m.group(1)):
                out["part_name"] = _clean(m.group(1).replace("_", " "))
        for key, lab in (
            ("material", r"(?:MATERIAL|MATL\.?)"),
            ("finish", r"(?:FINISH(?:\s*TREATMENT)?|SURFACE\s*FINISH|COATING)"),
            ("mass", r"(?:MASS|WEIGHT|WT\.?)"),
            ("revision", r"REV(?:ISION)?"),
            ("supplier_name", r"(?:SUPPLIER(?:\s*NAME)?|VENDOR)"),
            ("customer_name", r"(?:CUSTOMER(?:\s*NAME)?|CLIENT|BUYER)"),
            ("company_name", r"(?:COMPANY(?:\s*NAME)?|MANUFACTURER)"),
            ("serial_number", r"(?:SERIAL\s*(?:NO\.?|NUMBER)|SR\.?\s*NO\.?)"),
            ("report_date", r"(?:DATE|ISSUE\s*DATE|DWG\.?\s*DATE)"),
            ("authorized_signatory", r"(?:AUTHORIZED\s*SIGNATORY|AUTHORISED\s*SIGNATORY|APPROVED\s*BY)"),
            ("part_name", r"(?:PART\s*NAME|NOMENCLATURE|DESCRIPTION|TITLE)"),
        ):
            if out.get(key):
                continue
            m = re.match(rf"(?i)^{lab}\s*:?\s*(.+)$", line)
            if m and _valid_value(key, m.group(1)):
                out[key] = _clean(m.group(1))
            elif re.match(rf"(?i)^{lab}\s*:?$", line):
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt and _valid_value(key, nxt):
                    out[key] = nxt
    # Long drawing/part codes anywhere (prefer longer digit runs)
    if not out.get("part_number"):
        codes = re.findall(r"\b(\d{8,}(?:[-./][A-Z0-9]+)*|[A-Z]{2,}\d{4,}[A-Z0-9./-]*)\b", full_text or "")
        codes = [c for c in codes if _valid_value("part_number", c)]
        if codes:
            out["part_number"] = max(codes, key=len)
    # Finish-like tokens wrongly used as part name
    if out.get("part_name") and re.search(r"(?i)^(uncoated|coated|painted|plated|rel)$", out["part_name"]):
        if not out.get("finish"):
            out["finish"] = out["part_name"]
        out["part_name"] = ""
    return out


def _page_plain_text(page: fitz.Page) -> str:
    text = page.get_text("text") or ""
    if len(text.strip()) >= 80:
        return text
    try:
        from app.services.autoballoon_engine import _resolve_tessdata

        td = _resolve_tessdata()
        if not td:
            return text
        tp = page.get_textpage_ocr(flags=0, language="eng", dpi=180, tessdata=td)
        ocr = page.get_text("text", textpage=tp) or ""
        return ocr if len(ocr.strip()) > len(text.strip()) else text
    except Exception:
        return text


def _extract_from_page(page: fitz.Page) -> dict[str, str]:
    found: dict[str, str] = {}
    items = _page_title_items(page)
    full_text = _page_plain_text(page)
    for i, (text, rect, _sz) in enumerate(items):
        for key, pat in _COMBINED_PATTERNS:
            if found.get(key):
                continue
            m = pat.match(text)
            if m:
                val = _clean(m.group(1))
                if _valid_value(key, val):
                    found[key] = val
        for key, pat in _LABEL_PATTERNS:
            if found.get(key):
                continue
            if pat.match(text):
                val = _find_nearby_value(items, rect, i, key)
                if val and _valid_value(key, val):
                    found[key] = val
    found = _line_fallback(full_text, found)
    company = _guess_company(items, full_text)
    if company and (not found.get("company_name") or len(company) > len(found.get("company_name") or "")):
        found["company_name"] = company
    return found


def extract_title_block_meta(pdf_path: Path) -> dict[str, str]:
    """Return title-block fields scraped from the drawing PDF (best effort)."""
    result = {k: "" for k in _META_KEYS}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return result
    try:
        if not doc.page_count:
            return result
        order = [doc.page_count - 1]
        if doc.page_count > 1:
            order.append(0)
        if doc.page_count > 2:
            order.append(doc.page_count - 2)
        for pi in order:
            page_found = _extract_from_page(doc[pi])
            for k, v in page_found.items():
                if v and not result.get(k):
                    result[k] = v
            if sum(1 for v in result.values() if v) >= 4:
                break
    finally:
        doc.close()
    return result


def merge_title_meta(meta: dict, extracted: dict[str, str]) -> None:
    """Fill empty job meta fields from extracted title block (never overwrite user edits)."""
    for key in _META_KEYS:
        cur = str(meta.get(key) or "").strip()
        if cur and not _valid_value(key, cur):
            meta[key] = ""
            cur = ""
        if not cur:
            val = str(extracted.get(key) or "").strip()
            if val and _valid_value(key, val):
                meta[key] = val
