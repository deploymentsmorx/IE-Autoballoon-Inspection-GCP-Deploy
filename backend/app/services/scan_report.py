"""Read a scanned inspection sheet into report fields the operator can correct."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import fitz

from app.services.autoballoon_engine import _resolve_tessdata

_HEADER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("part_number", re.compile(r"(?i)\b(?:part|drawing|drg|dwg|item)\s*(?:no\.?|number|#)\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})")),
    ("part_name", re.compile(r"(?i)\b(?:part\s*name|description|nomenclature)\b\s*[:\-]\s*(.{2,80})")),
    ("revision", re.compile(r"(?i)\b(?:rev(?:ision)?)\b\s*[:\-]?\s*([A-Z0-9]{1,8})")),
    ("material", re.compile(r"(?i)\b(?:material|matl)\b\s*[:\-]\s*(.{2,80})")),
    ("mass", re.compile(r"(?i)\b(?:mass|weight)\b\s*[:\-]\s*([0-9.]+\s*[a-zA-Z]*)")),
    ("finish", re.compile(r"(?i)\b(?:finish|coating|treatment)\b\s*[:\-]\s*(.{2,80})")),
    ("serial_number", re.compile(r"(?i)\b(?:serial|s\s*/\s*n|s/?n)\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9._/\-]{1,})")),
    ("report_date", re.compile(r"(?i)\b(?:date|inspection\s*date)\b\s*[:\-]\s*([0-9]{1,4}[./-][0-9]{1,2}[./-][0-9]{1,4}|[0-9]{4}-[0-9]{2}-[0-9]{2})")),
    ("company_name", re.compile(r"(?i)\b(?:company|manufacturer|mfr)\b\s*[:\-]\s*(.{2,80})")),
    ("supplier_name", re.compile(r"(?i)\b(?:supplier|vendor)\b\s*[:\-]\s*(.{2,80})")),
    ("customer_name", re.compile(r"(?i)\b(?:customer|client)\b\s*[:\-]\s*(.{2,80})")),
    ("authorized_signatory", re.compile(r"(?i)\b(?:inspector|signatory|checked\s*by|approved\s*by)\b\s*[:\-]\s*([A-Za-z][A-Za-z .'-]{1,60})")),
]

_ROW_RE = re.compile(
    r"^\s*(\d{1,4})\s+(.+?)\s+([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)?\s*([-+]?\d+(?:\.\d+)?)?\s*"
    r"([-+]?\d+(?:\.\d+)?)?\s*(.*?)\s*$"
)

_EMPTY_HEADER = {
    "part_number": "",
    "part_name": "",
    "revision": "",
    "material": "",
    "mass": "",
    "finish": "",
    "serial_number": "",
    "report_date": "",
    "company_name": "",
    "supplier_name": "",
    "customer_name": "",
    "authorized_signatory": "",
}


def _clean(value: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text[:limit].strip(" :|")


def page_text(page: fitz.Page) -> str:
    text = page.get_text("text") or ""
    if len(text.strip()) >= 40:
        return text
    tessdata = _resolve_tessdata()
    if not tessdata:
        return text
    try:
        tp = page.get_textpage_ocr(flags=0, language="eng", dpi=150, tessdata=tessdata)
        ocr = page.get_text("text", textpage=tp) or ""
    except Exception:
        return text
    return ocr if len(ocr.strip()) > len(text.strip()) else text


def read_document_text(pdf_path: Path, max_pages: int = 8) -> tuple[str, bool]:
    import time

    doc = fitz.open(pdf_path)
    try:
        parts: list[str] = []
        used_ocr = False
        started = time.monotonic()
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            before = page.get_text("text") or ""
            if len(before.strip()) >= 40:
                got = before
            elif time.monotonic() - started > 25:
                got = before
            else:
                got = page_text(page)
            if len(got.strip()) > len(before.strip()) + 10:
                used_ocr = True
            if got.strip():
                parts.append(got)
        return "\n".join(parts), used_ocr
    finally:
        doc.close()


def page_jpegs(pdf_path: Path, max_pages: int = 3) -> list[str]:
    doc = fitz.open(pdf_path)
    try:
        images: list[str] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            rect = page.rect
            longest = max(float(rect.width), float(rect.height)) or 1
            scale = min(1.6, 1400 / longest)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            images.append(base64.b64encode(pix.tobytes("jpeg", jpg_quality=60)).decode("ascii"))
        return images
    finally:
        doc.close()


_NUM = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_INSTR_RE = re.compile(
    r"(?i)\b("
    r"vernier(?:\s*caliper)?|dial\s*caliper|digital\s*caliper|caliper|"
    r"micrometer|micrometre|inside\s*micrometer|depth\s*micrometer|"
    r"height\s*gau?ge|bore\s*gau?ge|pin\s*gau?ge|thread\s*gau?ge|"
    r"radius\s*gau?ge|feeler\s*gau?ge|plug\s*gau?ge|snap\s*gau?ge|"
    r"air\s*gau?ge|dial\s*indicator|cmm|profile\s*projector|"
    r"vision\s*system|surface\s*(?:roughness|tester)|steel\s*rule|"
    r"bevel\s*protractor|comparator|go\s*/?\s*no[\s\-]*go"
    r")\b(?:\s*[A-Za-z0-9/\-]{0,24})?"
)
_UNIT_RE = re.compile(r"(?i)^(mm|cm|m|inch|in|deg|°|micron|µm|um)$")
_ID_RE = re.compile(r"(?i)^(?:[A-Z]{2,4}[-_/]\d{1,5}|[A-Z]{1,3}\d{3,6})$")


def _instrument_from_texts(texts: list[str]) -> tuple[str, str, list[str]]:
    blob = " ".join(texts)
    match = _INSTR_RE.search(blob)
    instrument = _clean(match.group(0), 40) if match else ""
    leftover: list[str] = []
    instr_id = ""
    if match:
        # Drop the matched instrument tokens from leftover text.
        matched = set(re.findall(r"[A-Za-z0-9]+", match.group(0).lower()))
        for t in texts:
            tl = t.lower()
            if any(part and part in matched for part in re.findall(r"[A-Za-z0-9]+", tl)):
                continue
            leftover.append(t)
    else:
        leftover = list(texts)
    for t in leftover:
        if _UNIT_RE.match(t):
            continue
        if _ID_RE.match(t) and not instr_id:
            instr_id = t
    remarks_bits = [t for t in leftover if t != instr_id and not _UNIT_RE.match(t)]
    return instrument, instr_id, remarks_bits


def _row_dict(
    balloon: int,
    *,
    reference: str = "",
    nominal: str = "",
    tol_low: str = "",
    tol_high: str = "",
    instrument: str = "",
    unit: str = "mm",
    instr_id: str = "",
    measured: list[str] | None = None,
    remarks: str = "",
    page: int = 1,
) -> dict[str, Any]:
    values = [_clean(v, 40) for v in (measured or []) if str(v or "").strip()]
    return {
        "sno": balloon,
        "balloon": balloon,
        "page": page,
        "reference": _clean(reference, 40),
        "nominal": _clean(nominal, 40),
        "tol_low": _clean(tol_low, 20),
        "tol_high": _clean(tol_high, 20),
        "instrument": _clean(instrument, 40),
        "unit": _clean(unit, 12) or "mm",
        "instr_id": _clean(instr_id, 40),
        "measured": values or [""],
        "remarks": _clean(remarks, 80),
    }


def _parse_row_line(line: str) -> dict[str, Any] | None:
    raw = (line or "").strip()
    if not raw or len(raw) < 3:
        return None
    if re.search(r"(?i)balloon|nominal|characteristic|item\s*no|part\s*number|inspection\s*report|instrument", raw) and not _NUM.match(
        raw.split()[0]
    ):
        return None
    parts = [p.strip() for p in re.split(r"[\t|;,]+|\s{2,}", raw) if p.strip()]
    if len(parts) < 2:
        parts = raw.split()
    if not parts or not re.fullmatch(r"\d{1,4}", parts[0]):
        return None
    balloon = int(parts[0])
    if balloon < 1 or balloon > 9999:
        return None
    # Printed report often repeats S.No then Balloon No.
    rest = parts[1:]
    if rest and re.fullmatch(r"\d{1,4}", rest[0]) and int(rest[0]) == balloon:
        rest = rest[1:]
    nums = [p for p in rest if _NUM.match(p)]
    texts = [p for p in rest if not _NUM.match(p)]
    instrument, instr_id, remark_bits = _instrument_from_texts(texts)
    unit = next((t for t in texts if _UNIT_RE.match(t)), "mm")
    reference_bits = [t for t in remark_bits if t not in {instrument, instr_id, unit}]
    if not nums and not instrument and not instr_id:
        return None
    extras = nums[:]
    # Typical order: nominal, tol_low, tol_high, measured...
    if len(extras) >= 3:
        nominal, tol_low, tol_high = extras[0], extras[1], extras[2]
        measured_vals = extras[3:]
    elif len(extras) == 2:
        nominal, measured_vals, tol_low, tol_high = extras[0], [extras[1]], "", ""
    elif len(extras) == 1:
        nominal, measured_vals, tol_low, tol_high = extras[0], [], "", ""
    else:
        nominal, measured_vals, tol_low, tol_high = "", [], "", ""
    match = _ROW_RE.match(raw)
    if match and not instrument:
        nominal = _clean(match.group(3), 40) or nominal
        tol_low = _clean(match.group(4) or "", 20) or tol_low
        tol_high = _clean(match.group(5) or "", 20) or tol_high
        extra_m = _clean(match.group(6) or "", 40)
        if extra_m and extra_m not in measured_vals:
            measured_vals = [extra_m, *measured_vals]
        if not reference_bits:
            reference_bits = [_clean(match.group(2), 40)]
    return _row_dict(
        balloon,
        reference=" ".join(reference_bits),
        nominal=nominal,
        tol_low=tol_low,
        tol_high=tol_high,
        instrument=instrument,
        unit=unit,
        instr_id=instr_id,
        measured=measured_vals,
        remarks=_clean(match.group(7) if match else "", 80),
    )


def _page_words(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    words = list(page.get_text("words") or [])
    return [(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])) for w in words]


def _cluster_word_rows(
    words: list[tuple[float, float, float, float, str]], y_tol: float = 6.0
) -> list[list[tuple[float, float, float, float, str]]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w[1], w[0]))
    rows: list[list[tuple[float, float, float, float, str]]] = []
    current: list[tuple[float, float, float, float, str]] = []
    y0: float | None = None
    for w in ordered:
        if y0 is None or abs(w[1] - y0) <= y_tol:
            current.append(w)
            if y0 is None:
                y0 = w[1]
        else:
            rows.append(sorted(current, key=lambda x: x[0]))
            current = [w]
            y0 = w[1]
    if current:
        rows.append(sorted(current, key=lambda x: x[0]))
    return rows


_HEADER_KEYS = (
    ("balloon", ("balloon", "bno", "b.no", "ball")),
    ("instrument", ("instrument", "instrment", "gage", "gauge")),
    ("unit", ("unit",)),
    ("instr_id", ("instr", "id", "inst.id")),
    ("nominal", ("nominal", "spec")),
    ("tol_low", ("tol", "low", "lower")),
    ("tol_high", ("high", "upper")),
    ("remarks", ("remark", "comment")),
    ("sno", ("s.no", "sno", "sl")),
)


def _header_key(text: str) -> str | None:
    t = re.sub(r"[^a-z0-9.]+", "", text.lower())
    if re.fullmatch(r"m\d+", t) or t in {"actual", "measured", "reading"}:
        return "measured"
    for key, aliases in _HEADER_KEYS:
        if any(a.replace(" ", "") in t for a in aliases):
            return key
    return None


def parse_pdf_table(pdf_path: Path, max_pages: int = 8) -> list[dict[str, Any]]:
    """Read balloon / instrument / measured cells from a printed inspection-report PDF."""
    doc = fitz.open(pdf_path)
    found: dict[int, dict[str, Any]] = {}
    try:
        for page_i, page in enumerate(doc):
            if page_i >= max_pages:
                break
            rows = _cluster_word_rows(_page_words(page))
            col_map: list[tuple[float, float, str]] = []
            for line in rows:
                texts = [w[4] for w in line]
                joined = " ".join(texts).lower()
                if "instrument" in joined and ("balloon" in joined or "nominal" in joined):
                    col_map = []
                    for w in line:
                        key = _header_key(w[4])
                        if key:
                            col_map.append((w[0], w[2], key))
                    continue
                parsed = _parse_row_line(" ".join(texts))
                if not parsed:
                    continue
                if col_map:
                    buckets: dict[str, list[str]] = {}
                    measured_bits: list[str] = []
                    for w in line:
                        cx = (w[0] + w[2]) / 2
                        best = min(col_map, key=lambda c: min(abs(cx - c[0]), abs(cx - c[1]), abs(cx - (c[0] + c[1]) / 2)))
                        key = best[2]
                        if key == "measured":
                            measured_bits.append(w[4])
                        else:
                            buckets.setdefault(key, []).append(w[4])
                    inst = " ".join(buckets.get("instrument") or [])
                    if inst:
                        parsed["instrument"] = _clean(inst, 40)
                    if buckets.get("instr_id"):
                        parsed["instr_id"] = _clean(" ".join(buckets["instr_id"]), 40)
                    if buckets.get("unit"):
                        parsed["unit"] = _clean(" ".join(buckets["unit"]), 12) or parsed["unit"]
                    if buckets.get("remarks"):
                        parsed["remarks"] = _clean(" ".join(buckets["remarks"]), 80)
                    extra_m = [t for t in measured_bits if _NUM.match(t)]
                    if extra_m:
                        parsed["measured"] = [_clean(v, 40) for v in extra_m]
                    parsed["page"] = page_i + 1
                balloon = int(parsed["balloon"])
                prev = found.get(balloon)
                if prev:
                    if not prev.get("instrument") and parsed.get("instrument"):
                        prev["instrument"] = parsed["instrument"]
                    if not prev.get("instr_id") and parsed.get("instr_id"):
                        prev["instr_id"] = parsed["instr_id"]
                    prev_m = [v for v in (prev.get("measured") or []) if str(v).strip()]
                    new_m = [v for v in (parsed.get("measured") or []) if str(v).strip()]
                    if len(new_m) > len(prev_m):
                        prev["measured"] = parsed["measured"]
                    if not prev.get("remarks") and parsed.get("remarks"):
                        prev["remarks"] = parsed["remarks"]
                else:
                    found[balloon] = parsed
    finally:
        doc.close()
    return [found[k] for k in sorted(found)]


def parse_inspection_text(text: str) -> dict[str, Any]:
    header = dict(_EMPTY_HEADER)
    for key, pattern in _HEADER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            header[key] = _clean(match.group(1))
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in (text or "").splitlines():
        parsed = _parse_row_line(line.strip())
        if not parsed:
            continue
        balloon = int(parsed["balloon"])
        if balloon in seen:
            continue
        seen.add(balloon)
        parsed["sno"] = len(rows) + 1
        rows.append(parsed)
    return {**header, "rows": rows}


def apply_suggestion_to_meta(meta: dict[str, Any], suggestion: dict[str, Any]) -> None:
    """Fill empty job report cells from a scan. Never wipe drawing balloons or saved values."""
    for key in _EMPTY_HEADER:
        if not str(meta.get(key) or "").strip() and suggestion.get(key):
            meta[key] = suggestion[key]
    cols = meta.get("measure_columns")
    if not isinstance(cols, list) or not cols:
        cols = [
            {"id": "m1", "label": "M1", "values": {}},
            {"id": "m2", "label": "M2", "values": {}},
        ]
        meta["measure_columns"] = cols
    src_rows = suggestion.get("rows") if isinstance(suggestion.get("rows"), list) else []
    needed = 2
    for row in src_rows:
        measured = row.get("measured") if isinstance(row, dict) else None
        if isinstance(measured, list):
            needed = max(needed, len(measured))
    while len(cols) < needed:
        n = len(cols) + 1
        cols.append({"id": f"m{n}", "label": f"M{n}", "values": {}})
    existing = meta.get("report_rows")
    if not isinstance(existing, list):
        existing = []
    if not existing:
        balloons = meta.get("balloons") if isinstance(meta.get("balloons"), list) else []
        existing = [
            {
                "sno": i + 1,
                "balloon": b.get("balloon"),
                "page": b.get("page") or 1,
                "reference": f"P{b.get('page') or 1}",
                "nominal": b.get("text") or "",
                "tol_low": "",
                "tol_high": "",
                "instrument": "",
                "unit": "mm",
                "instr_id": "",
                "measured": ["", ""],
                "remarks": "",
            }
            for i, b in enumerate(balloons)
            if isinstance(b, dict) and b.get("balloon") is not None
        ]
    by_balloon: dict[int, dict[str, Any]] = {}
    for row in src_rows:
        if not isinstance(row, dict):
            continue
        try:
            by_balloon[int(row.get("balloon"))] = row
        except (TypeError, ValueError):
            continue

    def _fill(dst: str, src: Any) -> str:
        return str(dst or "").strip() or str(src or "").strip()

    merged: list[dict[str, Any]] = []
    have: set[int] = set()
    for row in existing:
        if not isinstance(row, dict):
            continue
        try:
            balloon = int(row.get("balloon"))
        except (TypeError, ValueError):
            merged.append(row)
            continue
        have.add(balloon)
        src = by_balloon.get(balloon)
        if not src:
            merged.append(row)
            continue
        incoming = src.get("measured")
        if not isinstance(incoming, list):
            incoming = [incoming] if incoming else []
        measured = list(row.get("measured") or [])
        while len(measured) < len(cols):
            measured.append("")
        for i, val in enumerate(incoming):
            text = str(val or "").strip()
            if not text:
                continue
            if i >= len(measured):
                measured.append(text)
            elif not str(measured[i] or "").strip():
                measured[i] = text
            if i < len(cols):
                values = cols[i].setdefault("values", {})
                key = str(balloon)
                if not str(values.get(key) or "").strip():
                    values[key] = text
        row.update(
            {
                "reference": _fill(row.get("reference") or "", src.get("reference")),
                "nominal": _fill(row.get("nominal") or "", src.get("nominal")),
                "tol_low": _fill(row.get("tol_low") or "", src.get("tol_low")),
                "tol_high": _fill(row.get("tol_high") or "", src.get("tol_high")),
                "instrument": _fill(row.get("instrument") or "", src.get("instrument")),
                "unit": _fill(row.get("unit") or "", src.get("unit")) or "mm",
                "instr_id": _fill(row.get("instr_id") or "", src.get("instr_id")),
                "remarks": _fill(row.get("remarks") or "", src.get("remarks")),
                "measured": measured,
            }
        )
        merged.append(row)
    for balloon, src in by_balloon.items():
        if balloon in have:
            continue
        incoming = src.get("measured")
        if not isinstance(incoming, list):
            incoming = [incoming] if incoming else ["", ""]
        for i, val in enumerate(incoming):
            text = str(val or "").strip()
            if text and i < len(cols):
                cols[i].setdefault("values", {})[str(balloon)] = text
        merged.append(
            {
                "sno": len(merged) + 1,
                "balloon": balloon,
                "page": src.get("page") or 1,
                "reference": src.get("reference") or "",
                "nominal": src.get("nominal") or "",
                "tol_low": src.get("tol_low") or "",
                "tol_high": src.get("tol_high") or "",
                "instrument": src.get("instrument") or "",
                "unit": src.get("unit") or "mm",
                "instr_id": src.get("instr_id") or "",
                "measured": incoming or ["", ""],
                "remarks": src.get("remarks") or "",
            }
        )
    for i, row in enumerate(merged):
        row["sno"] = i + 1
    meta["report_rows"] = merged
    meta["measure_columns"] = cols


def _normalize_suggestion(raw: dict[str, Any], fallback_text: str) -> dict[str, Any]:
    parsed = parse_inspection_text(fallback_text)
    header = dict(_EMPTY_HEADER)
    for key in header:
        header[key] = _clean(str(raw.get(key) or parsed.get(key) or ""))
    rows_in = raw.get("rows") if isinstance(raw.get("rows"), list) else parsed["rows"]
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(rows_in[:400]):
        if not isinstance(row, dict):
            continue
        try:
            balloon = int(row.get("balloon") or row.get("sno") or i + 1)
        except (TypeError, ValueError):
            balloon = i + 1
        measured = row.get("measured")
        if isinstance(measured, list):
            values = [_clean(str(v), 40) for v in measured[:6]]
        else:
            values = [_clean(str(measured or ""), 40)]
        while len(values) < 2:
            values.append("")
        rows.append(
            {
                "sno": i + 1,
                "balloon": balloon,
                "page": int(row.get("page") or 1),
                "reference": _clean(str(row.get("reference") or ""), 40),
                "nominal": _clean(str(row.get("nominal") or ""), 40),
                "tol_low": _clean(str(row.get("tol_low") or row.get("tolerance_low") or ""), 20),
                "tol_high": _clean(str(row.get("tol_high") or row.get("tolerance_high") or ""), 20),
                "instrument": _clean(str(row.get("instrument") or ""), 40),
                "unit": _clean(str(row.get("unit") or "mm"), 12) or "mm",
                "instr_id": _clean(str(row.get("instr_id") or ""), 40),
                "measured": values,
                "remarks": _clean(str(row.get("remarks") or ""), 80),
            }
        )
    if not rows:
        rows = parsed["rows"]
    return {**header, "rows": rows}


def _openai_extract(text: str, images: list[str]) -> dict[str, Any]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    prompt = (
        "Extract an inspection report from this scanned sheet. "
        "Return ONLY JSON with keys part_number, part_name, revision, material, mass, finish, "
        "serial_number, report_date, company_name, supplier_name, customer_name, authorized_signatory, "
        "and rows. Each row has balloon (integer), reference, nominal, tol_low, tol_high, instrument, "
        "unit, instr_id, measured (string or list of readings), remarks. "
        "Use empty strings when a field is not on the sheet. Do not invent values."
    )
    if text.strip():
        prompt += "\n\nOCR / text:\n" + text.strip()[:12000]
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images[:3]:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image}"},
            }
        )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You extract inspection-report fields from scans. Reply with JSON only.",
            },
            {"role": "user", "content": content},
        ],
    }
    from app.services.ai_fallback import _http_json

    data = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        payload,
    )
    raw = data["choices"][0]["message"]["content"]
    match = re.search(r"\{[\s\S]*\}", raw or "")
    parsed = json.loads(match.group(0) if match else raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI did not return a JSON object")
    return parsed


def _merge_row_lists(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_balloon: dict[int, dict[str, Any]] = {}
    for row in extra + primary:
        try:
            balloon = int(row.get("balloon"))
        except (TypeError, ValueError):
            continue
        prev = by_balloon.get(balloon)
        if not prev:
            by_balloon[balloon] = dict(row)
            continue
        for key in ("instrument", "instr_id", "unit", "nominal", "tol_low", "tol_high", "reference", "remarks"):
            if not str(prev.get(key) or "").strip() and str(row.get(key) or "").strip():
                prev[key] = row[key]
        prev_m = [v for v in (prev.get("measured") or []) if str(v or "").strip()]
        new_m = [v for v in (row.get("measured") or []) if str(v or "").strip()]
        if len(new_m) > len(prev_m):
            prev["measured"] = row.get("measured")
    return [by_balloon[k] for k in sorted(by_balloon)]


def extract_inspection_report(pdf_path: Path) -> dict[str, Any]:
    """Return a suggestion. Nothing here is the saved report until the operator saves it."""
    text, used_ocr = read_document_text(pdf_path)
    raw: dict[str, Any] = parse_inspection_text(text)
    line_rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    extractor = "ocr" if used_ocr else "text"
    if len(line_rows) < 3:
        table_rows = parse_pdf_table(pdf_path)
        if table_rows:
            raw["rows"] = _merge_row_lists(table_rows, line_rows)
            extractor = "table"
    suggestion = _normalize_suggestion(raw, text)
    filled_headers = sum(1 for key in _EMPTY_HEADER if suggestion.get(key))
    suggestion["extractor"] = extractor
    n = len(suggestion["rows"])
    suggestion["summary"] = f"Read {n} row(s) and {filled_headers} header field(s) using {extractor}."
    if n == 0:
        suggestion["summary"] = (
            "Could not read table rows from this file. Photograph the printed Report PDF table "
            "(Instrument column visible), keep it sharp and upright, then attach again."
        )
    else:
        suggestion["summary"] += " Review Instrument / measured cells, then Save report data."
    return suggestion
