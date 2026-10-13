"""CMQ_LAB inspection report (portrait, Ingenious Engineering CMM lab format) as PDF and Excel."""

from __future__ import annotations

import re

from app.services.master_report import (
    GridCell,
    SheetGrid,
    build_master_rows,
    default_master_fields,
    render_pdf,
    render_xlsx,
)
from app.services.report_pdf import REPORT_CERT_NO, _measure_cols

ROWS_PER_SHEET = 38
MIN_ACTUAL_COLS = 2

_TOL_SUFFIX = re.compile(r"\s*(±\s*[\d.,]+.*|\+\s*[\d.,]+\s*/\s*-\s*[\d.,]+.*|[+-][\d.,]+\s*/\s*[+-][\d.,]+.*)$")


def _dimension_only(spec: str) -> str:
    return _TOL_SUFFIX.sub("", spec or "").strip() or (spec or "").strip()


def _result(statuses: list[str]) -> str:
    seen = [s for s in statuses if s != "empty"]
    if not seen:
        return ""
    return "NOT OK" if "red" in seen else "OK"


def _sheet_grid(payload: dict, chunk, sheet_no: int, sheet_count: int, n_actual: int) -> SheetGrid:
    mf = {**default_master_fields(), **(payload.get("master_fields") or {})}
    a0 = 5  # first actual-dimension column
    res = a0 + n_actual
    rem = res + 1  # last column
    weights = [30, 95, 60, 45, 60] + [140 / n_actual] * n_actual + [50, 75]

    cells: list[GridCell] = []
    heights: list[float] = []

    def add(r, c0, c1, text, style, rowspan=1, status="empty"):
        cells.append(GridCell(r, c0, c1, text, style, rowspan, status))

    doc_no = mf["cmq_doc_no"]
    sheet_txt = f"Sheet {sheet_no:02d} of {sheet_count:02d}"
    heights.append(28)
    add(0, 0, res, mf["cmq_company"], "title")
    add(0, rem, rem, REPORT_CERT_NO, "cert")
    heights.append(20)
    add(1, 0, res, mf["cmq_title"], "subtitle")
    add(1, rem, rem, f"Doc No: {doc_no}\n{sheet_txt}" if doc_no else sheet_txt, "label")

    mid = a0  # right-hand label column
    header = [
        ("Part Name :", str(payload.get("part_name", "")), "Date :", str(payload.get("report_date", ""))),
        (
            "Drawing Number :",
            mf["cmq_drawing_no"] or str(payload.get("part_number", "")),
            "Qty :",
            mf["cmq_qty"],
        ),
        ("Part Number :", str(payload.get("part_number", "")), "Revision :", str(payload.get("revision", ""))),
    ]
    r = 2
    for l_label, l_val, r_label, r_val in header:
        heights.append(18)
        add(r, 0, 1, l_label, "label")
        add(r, 2, mid - 1, l_val, "value")
        add(r, mid, mid, r_label, "label")
        add(r, mid + 1, rem, r_val, "value")
        r += 1

    heights += [18, 16]
    add(r, 0, 0, "Sl.\nNo.", "head", 2)
    add(r, 1, 1, "Parameter\nDescription", "head", 2)
    add(r, 2, 3, "Specification", "head")
    add(r + 1, 2, 2, "Dim", "head")
    add(r + 1, 3, 3, "Tol", "head")
    add(r, 4, 4, "Instrument\nUsed", "head", 2)
    add(r, a0, res - 1, "Actual Dimension", "head")
    labels = [str(c.get("label") or "") for c in _measure_cols(payload)]
    labels = (labels + [""] * n_actual)[:n_actual]
    for i, label in enumerate(labels):
        add(r + 1, a0 + i, a0 + i, label, "head")
    add(r, res, res, "Result", "head", 2)
    add(r, rem, rem, "Remarks", "head", 2)
    r += 2

    for i in range(ROWS_PER_SHEET):
        heights.append(16)
        mrow = chunk[i] if i < len(chunk) else None
        if mrow:
            m = mrow.cells  # [bno, param, spec, zone, tol, instrument, instr_id, *actuals, remarks]
            actuals = m[7 : 7 + n_actual]
            values = [m[0], m[1], _dimension_only(m[2]), m[4], m[5], *actuals, _result(mrow.statuses), m[-1]]
        else:
            values = [""] * (rem + 1)
        for c, text in enumerate(values):
            status = "empty"
            if mrow and a0 <= c < res:
                status = mrow.statuses[c - a0]
            elif mrow and c == res and text:
                status = "red" if text == "NOT OK" else "green"
            add(r, c, c, text, "data", status=status)
        r += 1

    heights.append(22)
    add(r, 0, mid - 1, f"Inspected By: {mf['cmq_inspected_by']}", "sign")
    add(r, mid, rem, f"Approved By: {mf['cmq_approved_by']}", "sign")
    r += 1
    heights.append(30)
    add(r, 0, rem, f"Comments: {mf['cmq_comments']}", "label")

    return SheetGrid(col_weights=weights, row_heights=heights, cells=cells)


def _sheets(payload: dict) -> list[SheetGrid]:
    n_actual = max(MIN_ACTUAL_COLS, len(_measure_cols(payload)))
    mrows = build_master_rows(payload, n_actual)
    chunks = [mrows[i : i + ROWS_PER_SHEET] for i in range(0, len(mrows), ROWS_PER_SHEET)] or [[]]
    return [_sheet_grid(payload, c, i + 1, len(chunks), n_actual) for i, c in enumerate(chunks)]


def build_cmq_pdf(payload: dict) -> bytes:
    return render_pdf(_sheets(payload), landscape=False)


def build_cmq_xlsx(payload: dict) -> bytes:
    return render_xlsx(_sheets(payload), landscape=False)
