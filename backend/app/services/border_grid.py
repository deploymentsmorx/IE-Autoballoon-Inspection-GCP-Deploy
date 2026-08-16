"""Resolve balloon location to sheet border grid reference (e.g. B-4, C3)."""

from __future__ import annotations

import re
from pathlib import Path

import fitz


def _clean(text: str) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip().upper()


def _on_border(x: float, y: float, page_rect: fitz.Rect, margin_frac: float = 0.055) -> str | None:
    """Return which border edge a point sits on, if any."""
    short = min(page_rect.width, page_rect.height)
    margin = max(18.0, min(100.0, short * margin_frac))
    left = x < page_rect.x0 + margin
    right = x > page_rect.x1 - margin
    top = y < page_rect.y0 + margin
    bottom = y > page_rect.y1 - margin
    if left:
        return "left"
    if right:
        return "right"
    if top:
        return "top"
    if bottom:
        return "bottom"
    return None


def _collect_border_marks(page: fitz.Page) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """
    Collect letter marks (A,B,C…) and number marks (1,2,3…) from the sheet frame.
    Letters → typically horizontal axis (top/bottom); numbers → vertical (left/right).
    Returns (letters_as_(label, axis_pos), numbers_as_(label, axis_pos)).
    """
    page_rect = page.rect
    letters: list[tuple[str, float, str]] = []  # label, pos along edge, edge
    numbers: list[tuple[str, float, str]] = []

    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = _clean(span.get("text", ""))
                if not t:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                edge = _on_border(cx, cy, page_rect)
                if not edge:
                    continue
                if re.match(r"^[A-Z]$", t):
                    # Position along the long axis of that edge
                    pos = cx if edge in {"top", "bottom"} else cy
                    letters.append((t, pos, edge))
                elif re.match(r"^\d{1,2}$", t):
                    pos = cy if edge in {"left", "right"} else cx
                    numbers.append((t, pos, edge))

    # Prefer letters from top/bottom (X), numbers from left/right (Y)
    letter_marks = [(lab, pos) for lab, pos, edge in letters if edge in {"top", "bottom"}]
    if not letter_marks:
        letter_marks = [(lab, pos) for lab, pos, _ in letters]

    number_marks = [(lab, pos) for lab, pos, edge in numbers if edge in {"left", "right"}]
    if not number_marks:
        number_marks = [(lab, pos) for lab, pos, _ in numbers]

    # Dedupe by label keeping median-ish position
    def dedupe(marks: list[tuple[str, float]]) -> list[tuple[str, float]]:
        buckets: dict[str, list[float]] = {}
        for lab, pos in marks:
            buckets.setdefault(lab, []).append(pos)
        out = []
        for lab, positions in buckets.items():
            positions.sort()
            out.append((lab, positions[len(positions) // 2]))
        out.sort(key=lambda x: x[1])
        return out

    return dedupe(letter_marks), dedupe(number_marks)


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
    Return border-grid style reference for a balloon center, e.g. 'B-4' or 'C3'.
    Falls back to 'P{n}' if no frame marks are found.
    """
    letters, numbers = _collect_border_marks(page)
    # Letters track X (horizontal), numbers track Y (vertical) — standard ISO frame
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
