"""Resolve balloon location to sheet border grid reference (e.g. L-1, J-2).

Balloon coordinates from the engine match PyMuPDF page.rect / search_for space
(same as get_text words). Border letters/numbers are read from the outer frame;
the nearest letter + nearest number form the reference inspectors expect
(e.g. top-right corner → L-1 on Tata DRD sheets).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import fitz

log = logging.getLogger("border_grid")

# Cache per (pdf path, mtime, page)
_MARKS_CACHE: dict[tuple[str, float, int], tuple] = {}
_MARKS_CACHE_MAX = 64


def _clean(text: str) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip().upper()


def _parse_grid_token(t: str) -> tuple[str | None, str | None]:
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
    if re.match(r"^\d{1,2}$", t):
        n = int(t)
        if 1 <= n <= 24:
            return None, str(n)
    return None, None


def _dedupe_marks(marks: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Keep one position per label — prefer the median of a tight cluster (frame line)."""
    buckets: dict[str, list[float]] = {}
    for lab, pos in marks:
        buckets.setdefault(lab, []).append(pos)
    out: list[tuple[str, float]] = []
    for lab, positions in buckets.items():
        positions.sort()
        best_pos = positions[0]
        if len(positions) >= 2:
            med = positions[len(positions) // 2]
            near = [p for p in positions if abs(p - med) < 80]
            if near:
                best_pos = near[len(near) // 2]
        out.append((lab, best_pos))
    out.sort(key=lambda x: x[1])
    return out


def _prefer_sequential(marks: list[tuple[str, float]], *, kind: str) -> list[tuple[str, float]]:
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
        seq = [(lab, pos) for _, lab, pos in best]
        pos = [p for _, p in seq]
        keys = [k for k, _, _ in best]
        up = sum(1 for i in range(1, len(seq)) if keys[i] > keys[i - 1] and pos[i] > pos[i - 1])
        down = sum(1 for i in range(1, len(seq)) if keys[i] > keys[i - 1] and pos[i] < pos[i - 1])
        if down > up:
            seq = list(reversed(seq))
        return sorted(seq, key=lambda x: x[1])
    return marks


def _mark_quality(marks: list[tuple[str, float]], *, kind: str) -> tuple[int, list[tuple[str, float]]]:
    seq = _prefer_sequential(marks, kind=kind)
    if len(seq) < 2:
        return 0, seq
    if kind == "letter":
        keys = [ord(lab[0]) - ord("A") for lab, _ in seq]
    else:
        keys = [int(lab) for lab, _ in seq]
    positions = [pos for _, pos in seq]
    good = 0
    for i in range(1, len(seq)):
        dk = keys[i] - keys[i - 1]
        dp = positions[i] - positions[i - 1]
        if dp == 0:
            continue
        # Numbers increase along the edge; letters may run A→L or L→A (Tata / ISO borders)
        if kind == "letter":
            if dk != 0:
                good += 1
        elif dk * dp > 0:
            good += 1
    if good < max(1, len(seq) - 2):
        return 0, seq
    return len(seq) * 10 + good, seq


def _edge_of(cx: float, cy: float, w: float, h: float, *, frac: float = 0.05, outer: float = 0.55) -> str | None:
    m = max(14.0, min(140.0, min(w, h) * frac))
    o = m * outer
    if cy < m:
        return "top" if cy < o else None
    if cy > h - m:
        return "bottom" if cy > h - o else None
    if cx < m:
        return "left" if cx < o else None
    if cx > w - m:
        return "right" if cx > w - o else None
    return None


def _iter_border_words(page: fitz.Page) -> list[tuple[str, float, float, float]]:
    """Return (text, cx, cy, glyph_h) in page.rect space (same as balloon coords)."""
    out: list[tuple[str, float, float, float]] = []
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
        gh = abs(float(y1) - float(y0))
        if gh > 14:
            continue
        cx = (float(x0) + float(x1)) / 2
        cy = (float(y0) + float(y1)) / 2
        # Reject tokens outside the visible page (watermark / wrong-space noise)
        if cx < -2 or cy < -2 or cx > page.rect.width + 2 or cy > page.rect.height + 2:
            continue
        out.append((t, cx, cy, gh))
    return out


def _ocr_border_words(page: fitz.Page) -> list[tuple[str, float, float, float]]:
    """OCR only the four border strips — used when vector text is missing/weak."""
    try:
        from app.services.autoballoon_engine import _resolve_tessdata

        tess = _resolve_tessdata()
        if not tess:
            return []
    except Exception:
        return []

    out: list[tuple[str, float, float, float]] = []
    w, h = float(page.rect.width), float(page.rect.height)
    band = max(28.0, min(w, h) * 0.04)
    strips = [
        fitz.Rect(0, 0, w, band),
        fitz.Rect(0, h - band, w, h),
        fitz.Rect(0, 0, band, h),
        fitz.Rect(w - band, 0, w, h),
    ]
    for clip in strips:
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False)
            tmp = fitz.open()
            tpage = tmp.new_page(width=pix.width, height=pix.height)
            tpage.insert_image(tpage.rect, pixmap=pix)
            tp = tpage.get_textpage_ocr(dpi=150, full=True, language="eng", tessdata=tess)
            for b in tpage.get_text("words", textpage=tp) or []:
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
                t = _clean(str(text))
                if not t or len(t) > 3:
                    continue
                sx = clip.x0 + (float(x0) + float(x1)) / 2 / 2.0
                sy = clip.y0 + (float(y0) + float(y1)) / 2 / 2.0
                out.append((t, sx, sy, abs(float(y1) - float(y0)) / 2.0))
            tmp.close()
        except Exception as e:
            log.debug("border OCR strip failed: %s", e)
    return out


def _collect_display_marks(page: fitz.Page) -> tuple[
    list[tuple[str, float]],
    list[tuple[str, float]],
    str,
    str,
]:
    """
    Collect letter/number marks in page.rect space.

    Returns:
      letter_marks: (label, axis_pos)
      number_marks: (label, axis_pos)
      letter_axis: 'x' or 'y' — which page.rect axis to match against letter_marks
      number_axis: 'x' or 'y' — which page.rect axis to match against number_marks
    """
    w, h = float(page.rect.width), float(page.rect.height)
    words = _iter_border_words(page)
    letters_n = sum(1 for t, _, _, _ in words if re.match(r"^[A-Z]$", t))
    numbers_n = sum(1 for t, _, _, _ in words if re.match(r"^\d{1,2}$", t) and 1 <= int(t) <= 24)
    if letters_n < 4 or numbers_n < 4:
        ocr_words = _ocr_border_words(page)
        if ocr_words:
            words = words + ocr_words

    letters_by_edge: dict[str, list[tuple[str, float]]] = {"top": [], "bottom": [], "left": [], "right": []}
    numbers_by_edge: dict[str, list[tuple[str, float]]] = {"top": [], "bottom": [], "left": [], "right": []}

    # Outer-frame hug — reject inset dims / title-block digits
    hug = min(40.0, min(w, h) * 0.02)

    for t, cx, cy, _gh in words:
        edge = _edge_of(cx, cy, w, h)
        if not edge:
            continue
        letter, number = _parse_grid_token(t)
        if letter and not number:
            # Pure letter tokens only for letter axis (ignore "X4" style)
            pos = cx if edge in {"top", "bottom"} else cy
            letters_by_edge[edge].append((letter, pos))
        if number and not letter:
            if edge == "left" and cx > hug:
                continue
            if edge == "right" and cx < w - hug:
                continue
            if edge == "top" and cy > hug:
                continue
            if edge == "bottom" and cy < h - hug:
                continue
            pos = cy if edge in {"left", "right"} else cx
            numbers_by_edge[edge].append((number, pos))

    def best_edge(groups: dict[str, list[tuple[str, float]]], *, kind: str, prefer: list[str]):
        ranked = []
        for edge in prefer:
            ded = _dedupe_marks(groups.get(edge) or [])
            score, seq = _mark_quality(ded, kind=kind)
            ranked.append((score, edge, seq))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0] if ranked else (0, "", [])

    q_let, let_edge, seq_let = best_edge(
        letters_by_edge, kind="letter", prefer=["top", "bottom", "left", "right"]
    )
    q_num, num_edge, seq_num = best_edge(
        numbers_by_edge, kind="number", prefer=["left", "right", "top", "bottom"]
    )

    if q_let <= 0 or q_num <= 0:
        return seq_let, seq_num, "x", "y"

    # Letters along top/bottom → match by x; along left/right → match by y
    letter_axis = "x" if let_edge in {"top", "bottom"} else "y"
    number_axis = "x" if num_edge in {"top", "bottom"} else "y"

    return seq_let, seq_num, letter_axis, number_axis


def _cached_marks(page: fitz.Page, *, cache_key: tuple[str, float, int] | None):
    if cache_key is not None and cache_key in _MARKS_CACHE:
        return _MARKS_CACHE[cache_key]
    marks = _collect_display_marks(page)
    if cache_key is not None:
        if len(_MARKS_CACHE) >= _MARKS_CACHE_MAX:
            try:
                _MARKS_CACHE.pop(next(iter(_MARKS_CACHE)))
            except StopIteration:
                pass
        _MARKS_CACHE[cache_key] = marks
    return marks


def _marks_tuple_quality(marks: tuple) -> int:
    """Score grid usability; 0 = do not use (partial/noisy marks from drawing text)."""
    letter_marks, number_marks, _, _ = marks
    if len(letter_marks) < 8 or len(number_marks) < 8:
        return 0
    q_let, seq_let = _mark_quality(letter_marks, kind="letter")
    q_num, seq_num = _mark_quality(number_marks, kind="number")
    if q_let <= 0 or q_num <= 0:
        return 0
    return q_let + q_num + len(seq_let) + len(seq_num)


def _scale_marks_to_page(
    marks: tuple,
    *,
    from_w: float,
    from_h: float,
    to_w: float,
    to_h: float,
) -> tuple:
    """Scale a grid template from one page size to another (e.g. A0 → A1 half-size insert)."""
    letter_marks, number_marks, letter_axis, number_axis = marks
    sx = to_w / from_w if from_w > 0 else 1.0
    sy = to_h / from_h if from_h > 0 else 1.0

    def scale_list(mlist: list[tuple[str, float]], axis: str) -> list[tuple[str, float]]:
        s = sx if axis == "x" else sy
        return [(lab, pos * s) for lab, pos in mlist]

    return (
        scale_list(letter_marks, letter_axis),
        scale_list(number_marks, number_axis),
        letter_axis,
        number_axis,
    )


def _build_best_grid_template(
    doc: fitz.Document,
    *,
    path_key: str,
    mtime: float,
) -> tuple[tuple, float, float] | None:
    """Best full border grid found anywhere in the PDF (shared x/y zone axes)."""
    best: tuple[tuple, float, float] | None = None
    best_q = 0
    for page_i in range(doc.page_count):
        page = doc[page_i]
        marks = _cached_marks(page, cache_key=(path_key, mtime, page_i))
        q = _marks_tuple_quality(marks)
        if q > best_q:
            best_q = q
            best = (marks, float(page.rect.width), float(page.rect.height))
    return best


def _effective_marks_for_page(
    page: fitz.Page,
    local_marks: tuple,
    template: tuple[tuple, float, float] | None,
) -> tuple:
    """
    Use local border marks when the full A–L / 1–11 frame is readable;
    otherwise reuse the best grid from another page, scaled to this page size.
    """
    if _marks_tuple_quality(local_marks) > 0:
        return local_marks
    if template is None:
        return local_marks
    tpl_marks, tpl_w, tpl_h = template
    return _scale_marks_to_page(
        tpl_marks,
        from_w=tpl_w,
        from_h=tpl_h,
        to_w=float(page.rect.width),
        to_h=float(page.rect.height),
    )


def _zone_label(marks: list[tuple[str, float]], value: float) -> str:
    """
    Map coordinate to grid zone by column/row boundaries (midpoints between marks).
    More stable than nearest-centre when the dim sits near a zone line.
    """
    if not marks:
        return ""
    seq = sorted(marks, key=lambda x: x[1])
    if len(seq) == 1:
        return seq[0][0]
    if value <= (seq[0][1] + seq[1][1]) / 2.0:
        return seq[0][0]
    if value >= (seq[-2][1] + seq[-1][1]) / 2.0:
        return seq[-1][0]
    for i in range(len(seq) - 1):
        mid = (seq[i][1] + seq[i + 1][1]) / 2.0
        if value <= mid:
            return seq[i][0]
    return seq[-1][0]


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


def _dim_reference_point(b: dict) -> tuple[float, float]:
    """
    Grid zone from the dimension dotted-square (text box), never the balloon circle.
    Uses tx/ty (arrow anchor / dim centre) when stored, else centre of x,y,width,height.
    """
    if b.get("tx") is not None and b.get("ty") is not None:
        return float(b["tx"]), float(b["ty"])
    x = float(b.get("x") or 0)
    y = float(b.get("y") or 0)
    w = float(b.get("width") or 0)
    h = float(b.get("height") or 0)
    if w > 0 or h > 0:
        return x + w / 2.0, y + (h / 2.0 if h > 0 else 6.0)
    if b.get("bx") is not None and b.get("by") is not None:
        return float(b["bx"]), float(b["by"])
    return x, y


def _balloon_point(b: dict) -> tuple[float, float]:
    """Alias used by zone lookup — always the dim-box reference, not leader circle."""
    return _dim_reference_point(b)


def _ref_from_marks(
    letter_marks: list[tuple[str, float]],
    number_marks: list[tuple[str, float]],
    letter_axis: str,
    number_axis: str,
    x: float,
    y: float,
    *,
    page_no: int,
) -> str:
    lx = x if letter_axis == "x" else y
    nx = x if number_axis == "x" else y
    letter = _zone_label(letter_marks, lx)
    number = _zone_label(number_marks, nx)
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
    marks=None,
) -> str:
    if marks is None:
        letter_marks, number_marks, letter_axis, number_axis = _collect_display_marks(page)
    else:
        letter_marks, number_marks, letter_axis, number_axis = marks
    # x,y are already in page.rect space (same as balloon engine / search_for)
    return _ref_from_marks(
        letter_marks, number_marks, letter_axis, number_axis, x, y, page_no=page_no
    )


def zone_references_for_balloons(pdf_path: Path, balloons: list[dict]) -> dict[int, str]:
    """Map balloon number → zone reference (e.g. L-1)."""
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
        template = _build_best_grid_template(doc, path_key=path_key, mtime=mtime)
        page_cache: dict[int, tuple] = {}

        for b in balloons:
            try:
                bno = int(b.get("balloon") or 0)
                page_i = max(0, int(b.get("page") or 1) - 1)
                if page_i >= doc.page_count:
                    continue
                if page_i not in page_cache:
                    page = doc[page_i]
                    local = _cached_marks(page, cache_key=(path_key, mtime, page_i))
                    marks = _effective_marks_for_page(page, local, template)
                    page_cache[page_i] = (page, marks)

                _page, marks = page_cache[page_i]
                letter_marks, number_marks, letter_axis, number_axis = marks
                mx, my = _balloon_point(b)
                ref = _ref_from_marks(
                    letter_marks,
                    number_marks,
                    letter_axis,
                    number_axis,
                    mx,
                    my,
                    page_no=page_i + 1,
                )
                out[(page_i + 1, bno)] = ref
                out[bno] = ref  # continuous numbering (unique bno)
            except Exception:
                continue
    finally:
        doc.close()
    return out
