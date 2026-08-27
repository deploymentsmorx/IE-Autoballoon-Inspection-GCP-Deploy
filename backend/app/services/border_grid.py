"""Resolve balloon location to sheet border grid reference (e.g. B-4, C3)."""

from __future__ import annotations

import re
from pathlib import Path

import fitz

# Cache border marks per (pdf path, mtime, page) — scanning the frame once is enough.
_MARKS_CACHE: dict[
    tuple[str, float, int],
    tuple[list[tuple[str, float]], list[tuple[str, float]], str, str],
] = {}
_MARKS_CACHE_MAX = 64


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
    margin_frac: float = 0.045,
    outer_frac: float = 0.55,
) -> str | None:
    """
    Return which border edge a point sits on, if any.
    outer_frac keeps only the outer portion of the border band so drawing
    dimensions near the frame are ignored.
    """
    short = min(page_rect.width, page_rect.height)
    margin = max(16.0, min(120.0, short * margin_frac))
    outer = margin * max(0.25, min(1.0, outer_frac))
    if x < page_rect.x0 + margin:
        return "left" if x < page_rect.x0 + outer else None
    if x > page_rect.x1 - margin:
        return "right" if x > page_rect.x1 - outer else None
    if y < page_rect.y0 + margin:
        return "top" if y < page_rect.y0 + outer else None
    if y > page_rect.y1 - margin:
        return "bottom" if y > page_rect.y1 - outer else None
    return None


def _parse_grid_token(t: str) -> tuple[str | None, str | None]:
    """Parse border mark → (column letter, row number)."""
    t = _clean(t)
    if not t:
        return None, None
    m = re.match(r"^([A-Z])[-–]?(\d{1,2})$", t)
    if m:
        n = int(m.group(2))
        if 1 <= n <= 24:
            return m.group(1), str(n)
    if re.match(r"^[A-Z]$", t):
        return t, None
    # Sheet zone rows are small integers (1–24), never part numbers / dims
    if re.match(r"^\d{1,2}$", t):
        n = int(t)
        if 1 <= n <= 24:
            return None, str(n)
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


def _prefer_sequential(marks: list[tuple[str, float]], *, kind: str) -> list[tuple[str, float]]:
    """Keep the densest sequential run of letters (A,B,C…) or numbers (1,2,3…)."""
    if len(marks) < 2:
        return marks
    if kind == "letter":
        vals = [(ord(lab[0]) - ord("A"), lab, pos) for lab, pos in marks if lab and lab[0].isalpha()]
    else:
        vals = [(int(lab), lab, pos) for lab, pos in marks if str(lab).isdigit()]
    if len(vals) < 2:
        return marks
    vals.sort(key=lambda x: x[0])
    best: list[tuple[int, str, float]] = []
    cur: list[tuple[int, str, float]] = [vals[0]]
    for item in vals[1:]:
        if item[0] == cur[-1][0] + 1:
            cur.append(item)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [item]
    if len(cur) > len(best):
        best = cur
    if len(best) >= 2:
        return sorted([(lab, pos) for _, lab, pos in best], key=lambda x: x[1])
    return marks


def _mark_quality(marks: list[tuple[str, float]], *, kind: str) -> tuple[int, list[tuple[str, float]]]:
    """
    Score a candidate mark set. Prefer long sequential runs whose positions
    increase with the label (true frame marks). Random dims score poorly.
    """
    seq = _prefer_sequential(marks, kind=kind)
    if len(seq) < 2:
        return 0, seq
    if kind == "letter":
        keys = [ord(lab[0]) - ord("A") for lab, _ in seq]
    else:
        keys = [int(lab) for lab, _ in seq]
    positions = [pos for _, pos in seq]
    # Count how many adjacent pairs increase in both label and position
    good = 0
    for i in range(1, len(seq)):
        if keys[i] > keys[i - 1] and positions[i] > positions[i - 1]:
            good += 1
        elif keys[i] < keys[i - 1] and positions[i] < positions[i - 1]:
            good += 1  # reverse-ordered frame still OK
    # Require mostly monotonic; otherwise reject (noise)
    if good < max(1, len(seq) - 2):
        return 0, seq
    return len(seq) * 10 + good, seq


def _collect_border_marks(
    page: fitz.Page,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]], str, str]:
    """
    Collect column + row grid marks in mediabox space.

    Returns (letter_marks, number_marks, letter_axis, number_axis)
    where axis is 'x' or 'y' — which balloon coordinate the mark position maps to.
    On 90°/270° sheets, letters often sit on left/right (vary with y) and
    numbers on top/bottom (vary with x).
    """
    page_rect = _page_mediabox(page)
    letters: list[tuple[str, float, str]] = []
    numbers: list[tuple[str, float, str]] = []

    try:
        words = page.get_text("words") or []
    except Exception:
        words = []

    for w in words:
        try:
            x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        except Exception:
            continue
        t = _clean(str(text))
        if not t or len(t) > 3:
            continue
        glyph_h = abs(float(y1) - float(y0))
        # Border zone glyphs are small; skip title-block / large dims
        if glyph_h > 12.0:
            continue
        cx, cy = _to_mediabox(page, (float(x0) + float(x1)) / 2, (float(y0) + float(y1)) / 2)
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
        return _dedupe_marks(picked)

    letter_tb = from_edges(letters, {"top", "bottom"})
    letter_lr = from_edges(letters, {"left", "right"})
    number_lr = from_edges(numbers, {"left", "right"})
    number_tb = from_edges(numbers, {"top", "bottom"})

    q_ltb, seq_ltb = _mark_quality(letter_tb, kind="letter")
    q_llr, seq_llr = _mark_quality(letter_lr, kind="letter")
    q_nlr, seq_nlr = _mark_quality(number_lr, kind="number")
    q_ntb, seq_ntb = _mark_quality(number_tb, kind="number")

    if q_ltb >= q_llr and q_ltb > 0:
        letter_marks, letter_axis = seq_ltb, "x"
    elif q_llr > 0:
        letter_marks, letter_axis = seq_llr, "y"
    else:
        letter_marks, letter_axis = [], "x"

    if q_ntb >= q_nlr and q_ntb > 0:
        number_marks, number_axis = seq_ntb, "x"
    elif q_nlr > 0:
        number_marks, number_axis = seq_nlr, "y"
    else:
        number_marks, number_axis = [], "y"

    return letter_marks, number_marks, letter_axis, number_axis


def _cached_border_marks(
    page: fitz.Page,
    *,
    cache_key: tuple[str, float, int] | None,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]], str, str]:
    if cache_key is not None and cache_key in _MARKS_CACHE:
        return _MARKS_CACHE[cache_key]
    marks = _collect_border_marks(page)
    if cache_key is not None:
        if len(_MARKS_CACHE) >= _MARKS_CACHE_MAX:
            try:
                _MARKS_CACHE.pop(next(iter(_MARKS_CACHE)))
            except StopIteration:
                pass
        _MARKS_CACHE[cache_key] = marks
    return marks


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


def _zone_from_marks(
    letters: list[tuple[str, float]],
    numbers: list[tuple[str, float]],
    x: float,
    y: float,
    *,
    letter_axis: str = "x",
    number_axis: str = "y",
    page_no: int = 1,
) -> str:
    letter = _nearest_label(letters, x if letter_axis == "x" else y)
    number = _nearest_label(numbers, y if number_axis == "y" else x)
    if letter and number:
        return f"{letter}-{number}"
    if letter:
        return letter
    if number:
        return number
    return f"P{page_no}"


def zone_reference_for_point(
    page: fitz.Page,
    x: float,
    y: float,
    *,
    page_no: int = 1,
    marks: tuple[list[tuple[str, float]], list[tuple[str, float]], str, str] | None = None,
) -> str:
    """
    Border-grid reference for balloon / dim centre (mediabox x/y), e.g. 'B-4'.
    Falls back to 'P{n}' when frame marks are missing.
    """
    if marks is None:
        letters, numbers, letter_axis, number_axis = _collect_border_marks(page)
    else:
        letters, numbers, letter_axis, number_axis = marks
    return _zone_from_marks(
        letters,
        numbers,
        x,
        y,
        letter_axis=letter_axis,
        number_axis=number_axis,
        page_no=page_no,
    )


def _balloon_zone_point(b: dict) -> tuple[float, float]:
    """Use dimension tip centre for zone (characteristic location)."""
    w = float(b.get("width") or 0)
    h = float(b.get("height") or 0)
    x = b.get("x")
    y = b.get("y")
    if x is not None and y is not None:
        return float(x) + w / 2.0, float(y) + h / 2.0
    bx, by = b.get("bx"), b.get("by")
    if bx is not None and by is not None:
        return float(bx), float(by)
    return 0.0, 0.0


def zone_references_for_balloons(pdf_path: Path, balloons: list[dict]) -> dict[int, str]:
    """Map balloon number → zone reference string (marks scanned once per page)."""
    out: dict[int, str] = {}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return out
    try:
        try:
            mtime = float(Path(pdf_path).stat().st_mtime)
        except OSError:
            mtime = 0.0
        path_key = str(Path(pdf_path).resolve())
        page_marks: dict[
            int, tuple[list[tuple[str, float]], list[tuple[str, float]], str, str]
        ] = {}
        for b in balloons:
            try:
                bno = int(b.get("balloon") or 0)
                page_i = max(0, int(b.get("page") or 1) - 1)
                if page_i >= doc.page_count:
                    continue
                if page_i not in page_marks:
                    page = doc[page_i]
                    page_marks[page_i] = _cached_border_marks(
                        page,
                        cache_key=(path_key, mtime, page_i),
                    )
                letters, numbers, letter_axis, number_axis = page_marks[page_i]
                cx, cy = _balloon_zone_point(b)
                out[bno] = _zone_from_marks(
                    letters,
                    numbers,
                    cx,
                    cy,
                    letter_axis=letter_axis,
                    number_axis=number_axis,
                    page_no=page_i + 1,
                )
            except Exception:
                continue
    finally:
        doc.close()
    return out
