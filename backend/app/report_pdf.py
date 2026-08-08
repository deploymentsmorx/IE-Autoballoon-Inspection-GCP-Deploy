"""Generate FAI-style Inspection Report PDF (landscape) matching NP1213M layout."""

from __future__ import annotations

import io
import re
from pathlib import Path

import fitz

from .db import STATIC


def _parse_num(s: str) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if not t:
        return None
    m = re.search(r"[-+]?\d*\.?\d+", t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def measure_status(nominal: str, tol_low: str, tol_high: str, measured: str) -> str:
    """
    green = exact match to nominal
    yellow = within tolerance band
    red = outside tolerance / wrong
    empty = no measured value
    """
    m = _parse_num(measured)
    if m is None:
        return "empty"
    n = _parse_num(nominal)
    if n is None:
        return "empty"
    lo = _parse_num(tol_low)
    hi = _parse_num(tol_high)
    if abs(m - n) < 1e-9:
        return "green"
    if lo is not None or hi is not None:
        low = n + (lo if lo is not None else 0.0)
        high = n + (hi if hi is not None else 0.0)
        if lo is not None and hi is not None and lo > hi:
            low, high = n + hi, n + lo
        # If only one side given, treat as ± that side when both missing opposite
        if lo is None and hi is not None:
            low, high = n - abs(hi), n + abs(hi)
        if hi is None and lo is not None:
            low, high = n - abs(lo), n + abs(lo)
        if low <= m <= high:
            return "yellow"
        return "red"
    # No tolerance → any deviation is red
    return "red"


def build_report_pdf(payload: dict, logo_path: Path | None = None) -> bytes:
    logo_path = logo_path or (STATIC / "smorx-logo-full.png")
    page_w, page_h = 841.89, 595.28  # A4 landscape
    doc = fitz.open()
    rows = payload.get("rows") or []
    # Extra measure columns beyond the default Measured 1/2/3
    extra_cols = payload.get("measure_columns") or []

    headers = [
        "S.No", "Balloon\nNo.", "Reference", "Nominal", "Tol\n(low)", "Tol\n(high)",
        "Instrument", "Unit", "Instr. ID", "Measured 1", "Measured 2", "Measured 3", "Remarks",
    ]
    for col in extra_cols:
        headers.insert(-1, col.get("label") or "Measured")

    # column widths sum ~ page_w - margins
    margin = 28
    usable = page_w - 2 * margin
    base_weights = [28, 36, 70, 48, 36, 36, 70, 32, 55, 52, 52, 52, 70]
    # insert weights for extra cols before remarks
    weights = base_weights[:-1] + [52] * len(extra_cols) + base_weights[-1:]
    # trim headers/weights to match
    while len(weights) < len(headers):
        weights.insert(-1, 52)
    weights = weights[: len(headers)]
    total_w = sum(weights)
    col_w = [usable * w / total_w for w in weights]

    row_h = 16
    header_h = 28
    top_info_h = 78
    rows_per_page = int((page_h - margin - top_info_h - header_h - 20) // row_h)

    def draw_header_block(page: fitz.Page, first: bool):
        y = margin
        if logo_path.exists() and first:
            try:
                page.insert_image(fitz.Rect(margin, y, margin + 140, y + 36), filename=str(logo_path))
            except Exception:
                pass
        page.insert_text(
            fitz.Point(page_w / 2 - 60, y + 18),
            "Inspection Report",
            fontsize=14,
            fontname="helv",
            color=(0, 0, 0),
        )
        if not first:
            return top_info_h - 40
        meta = [
            ("Part Number", payload.get("part_number", "")),
            ("Part Name", payload.get("part_name", "")),
            ("Revision", payload.get("revision", "")),
            ("Material", payload.get("material", "")),
            ("Mass", payload.get("mass", "")),
            ("Finish Treatment", payload.get("finish", "")),
        ]
        y2 = y + 44
        x = margin
        for i, (lab, val) in enumerate(meta):
            page.insert_text(fitz.Point(x, y2), f"{lab}:", fontsize=8, fontname="helv", color=(0.2, 0.2, 0.2))
            page.insert_text(fitz.Point(x + 78, y2), str(val), fontsize=8, fontname="helv", color=(0, 0, 0))
            if i % 3 == 2:
                y2 += 14
                x = margin
            else:
                x += 260
        return top_info_h

    def status_color(status: str):
        return {
            "green": (0.75, 0.95, 0.75),
            "yellow": (1.0, 0.95, 0.65),
            "red": (1.0, 0.78, 0.78),
            "empty": (1, 1, 1),
        }.get(status, (1, 1, 1))

    for start in range(0, max(len(rows), 1), rows_per_page):
        page = doc.new_page(width=page_w, height=page_h)
        info_h = draw_header_block(page, first=(start == 0))
        y = margin + info_h
        x0 = margin
        # header row
        shape = page.new_shape()
        x = x0
        for i, h in enumerate(headers):
            r = fitz.Rect(x, y, x + col_w[i], y + header_h)
            shape.draw_rect(r)
            shape.finish(color=(0.15, 0.15, 0.15), fill=(0.92, 0.92, 0.94), width=0.4)
            page.insert_textbox(r + (2, 2, -2, -2), h, fontsize=7, fontname="helv", align=1)
            x += col_w[i]
        shape.commit()
        y += header_h

        chunk = rows[start : start + rows_per_page]
        if not chunk and start == 0:
            chunk = []
        for ri, row in enumerate(chunk):
            measured = list(row.get("measured") or ["", "", ""])
            while len(measured) < 3:
                measured.append("")
            # extra column values by balloon key
            extra_vals = []
            bkey = str(row.get("balloon"))
            for col in extra_cols:
                extra_vals.append((col.get("values") or {}).get(bkey, ""))

            cells = [
                str(row.get("sno", start + ri + 1)),
                str(row.get("balloon", "")),
                str(row.get("reference", "")),
                str(row.get("nominal", "")),
                str(row.get("tol_low", "")),
                str(row.get("tol_high", "")),
                str(row.get("instrument", "")),
                str(row.get("unit", "")),
                str(row.get("instr_id", "")),
                measured[0],
                measured[1],
                measured[2],
                *extra_vals,
                str(row.get("remarks", "")),
            ]
            # status for measured cells
            statuses = []
            for mv in measured[:3]:
                statuses.append(
                    measure_status(
                        str(row.get("nominal", "")),
                        str(row.get("tol_low", "")),
                        str(row.get("tol_high", "")),
                        mv,
                    )
                )
            for ev in extra_vals:
                statuses.append(
                    measure_status(
                        str(row.get("nominal", "")),
                        str(row.get("tol_low", "")),
                        str(row.get("tol_high", "")),
                        ev,
                    )
                )

            shape = page.new_shape()
            x = x0
            measure_start = 9
            measure_end = 9 + 3 + len(extra_vals)
            for i, cell in enumerate(cells):
                r = fitz.Rect(x, y, x + col_w[i], y + row_h)
                fill = (1, 1, 1)
                if measure_start <= i < measure_end:
                    fill = status_color(statuses[i - measure_start])
                shape.draw_rect(r)
                shape.finish(color=(0.3, 0.3, 0.3), fill=fill, width=0.3)
                page.insert_textbox(
                    r + (1, 3, -1, -1),
                    cell,
                    fontsize=7,
                    fontname="helv",
                    align=1,
                )
                x += col_w[i]
            shape.commit()
            y += row_h

    buf = io.BytesIO()
    doc.save(buf, deflate=True, garbage=3)
    doc.close()
    return buf.getvalue()


def characteristics_to_report_rows(chars: list[dict]) -> list[dict]:
    rows = []
    for i, c in enumerate(chars, start=1):
        text = str(c.get("text") or "")
        nominal = text
        tol_low = ""
        tol_high = ""
        m = re.search(r"([-+]?\d+(?:[.,]\d+)?)\s*±\s*([-+]?\d+(?:[.,]\d+)?)", text)
        if m:
            nominal = m.group(1).replace(",", ".")
            t = m.group(2).replace(",", ".")
            tol_low = f"-{t}"
            tol_high = t
        else:
            m2 = re.search(r"[-+]?\d+(?:[.,]\d+)?", text)
            if m2:
                nominal = m2.group(0).replace(",", ".")
        rows.append({
            "sno": i,
            "balloon": int(c.get("balloon") or i),
            "reference": f"P{c.get('page', 1)}",
            "nominal": "" if not text or re.match(r"(?i)^auto\s*balloo?n$", text.strip()) else nominal,
            "tol_low": tol_low,
            "tol_high": tol_high,
            "instrument": "",
            "unit": "mm",
            "instr_id": "",
            "measured": ["", "", ""],
            "remarks": "",
        })
    return rows
