"""Resolve balloon location to sheet border grid reference (e.g. B-4, C3)."""

from __future__ import annotations

import re
from pathlib import Path

import fitz


def _clean(text: str) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip().upper()


def _page_mediabox(page: fitz.Page) -> fitz.Rect:
    """Balloon x/y and CAD geometry use unrotated mediabox space."""
    return fitz.Rect(page.mediabox)


def _to_mediabox(page: fitz.Page, x: float, y: float) -> tuple[float, float]:
    """Map a point from page display space into mediabox (draw) space."""
    pt = fitz.Point(x, y) * page.derotation_matrix
    return float(pt.x), float(pt.y)


def _on_border(
    x: float,
    y: float,
    page_rect: fitz.Rect,
    *,
    margin_frac: float = 0.048,
) -> str | None:
    """Return which border edge a point sits on, if any."""
    short = min(page_rect.width, page_rect.height)
    margin = max(22.0, min(110.0, short * margin_frac))
    if x < page_rect.x0 + margin:
        return "left"
    if x > page_rect.x1 - margin:
        return "right"
    if y < page_rect.y0 + margin:
        return "top"
    if y > page_rect.y1 - margin:
        return "bottom"
    return None


def _parse_grid_token(t: str) -> tuple[str | None, str | None]:
    """Parse border mark → (column letter, row number)."""
    t = _clean(t)
    if not t:
        return None, None
    m = re.match(r"^([A-Z])(\d{1,3})$", t)
    if m:
        return m.group(1), m.group(2).lstrip("0") or "0"
    if re.match(r"^[A-Z]$", t):
        return t, None
    if re.match(r"^\d{1,3}$", t):
        return None, t.lstrip("0") or "0"
    return None, None


def _dedupe_marks(marks: list[tuple[str, float]]) -> list[tuple[str, float]]:
    buckets: dict[str, list[float]] = {}
    for lab, pos in marks:
        buckets.setdefault(lab, []).append(pos)
    out: list[tuple[str, float]] = []
    for lab, positions in buckets.items():
        positions.sort()
        out.append((lab, positions[len(positions) // 2]))
    out.sort(key=lambda x: x[1])
    return out


def _collect_border_marks(page: fitz.Page) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """
    Collect column (X) and row (Y) grid marks from the sheet frame in mediabox space.
    """
    page_rect = _page_mediabox(page)
    letters: list[tuple[str, float, str]] = []
    numbers: list[tuple[str, float, str]] = []

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = _clean(span.get("text", ""))
                if not t:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                cx, cy = _to_mediabox(page, (x0 + x1) / 2, (y0 + y1) / 2)
                edge = _on_border(cx, cy, page_rect)
                if not edge:
                    continue

                letter, number = _parse_grid_token(t)
                if letter:
                    pos = cx if edge in {"top", "bottom"} else cy
                    letters.append((letter, pos, edge))
                if number:
                    pos = cy if edge in {"left", "right"} else cx
                    numbers.append((number, pos, edge))

    def from_edges(
        marks: list[tuple[str, float, str]],
        edges: set[str],
    ) -> list[tuple[str, float]]:
        picked = [(lab, pos) for lab, pos, edge in marks if edge in edges]
        if len(picked) >= 2:
            return _dedupe_marks(picked)
        return _dedupe_marks([(lab, pos) for lab, pos, _ in marks])

    # ISO: letters along top/bottom (X), numbers along left/right (Y)
    letter_marks = from_edges(letters, {"top", "bottom"})
    number_marks = from_edges(numbers, {"left", "right"})

    # Alternate: numbers on top/bottom, letters on left/right
    if len(letter_marks) < 2:
        alt = from_edges(numbers, {"top", "bottom"})
        if len(alt) >= 2:
            letter_marks = alt
    if len(number_marks) < 2:
        alt = from_edges(letters, {"left", "right"})
        if len(alt) >= 2:
            number_marks = alt

    return letter_marks, number_marks


def _nearest_label(marks: list[tuple[str, float]], value: float) -> str:
    if not marks:
        return ""
    best = marks[0][0]
    best_d = abs(marks[0][1] - value)
    for lab, pos in marks[1:]:
        d = abs(pos - value)
        if d < best_d:
            best_d = d
            best = lab
    return best


def zone_reference_for_point(
    page: fitz.Page,
    x: float,
    y: float,
    *,
    page_no: int = 1,
) -> str:
    """
    Border-grid reference for balloon centre (mediabox x/y), e.g. 'B-4'.
    Falls back to 'P{n}' when frame marks are missing.
    """
    letters, numbers = _collect_border_marks(page)
    letter = _nearest_label(letters, x)
    number = _nearest_label(numbers, y)
    if letter and number:
        return f"{letter}-{number}"
    if letter:
        return letter
    if number:
        return number
    return f"P{page_no}"


def zone_references_for_balloons(pdf_path: Path, balloons: list[dict]) -> dict[int, str]:
    """Map balloon number → zone reference string."""
    out: dict[int, str] = {}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return out
    try:
        page_cache: dict[int, fitz.Page] = {}
        for b in balloons:
            try:
                bno = int(b.get("balloon") or 0)
                page_i = max(0, int(b.get("page") or 1) - 1)
                if page_i >= doc.page_count:
                    continue
                if page_i not in page_cache:
                    page_cache[page_i] = doc[page_i]
                page = page_cache[page_i]
                cx = float(b.get("x") or 0) + float(b.get("width") or 0) / 2
                cy = float(b.get("y") or 0) + float(b.get("height") or 0) / 2
                out[bno] = zone_reference_for_point(page, cx, cy, page_no=page_i + 1)
            except Exception:
                continue
    finally:
        doc.close()
    return out
