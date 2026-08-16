"""
Human-in-the-loop learning store for auto-balloon.

When a user manually balloons a missed dimension (or marks noise to ignore),
we persist the example. Later runs:
  1) auto-accept texts learned as dimensions (vector path)
  2) auto-skip texts learned as ignore
  3) feed recent examples to the AI fallback as few-shot context
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "learned_feedback.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    t = (text or "").replace("\xb1", "±").replace("\xb0", "°")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def store_path() -> Path:
    return _STORE_PATH


def load_feedback() -> list[dict[str, Any]]:
    path = _STORE_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("examples") or []
    return list(data) if isinstance(data, list) else []


def save_feedback(examples: list[dict[str, Any]]) -> None:
    path = _STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": _now(),
        "examples": examples,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def add_feedback(
    *,
    text: str,
    label: str,
    page: int | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    neighbors: str = "",
    source: str = "manual",
    drawing_hint: str = "",
) -> dict[str, Any]:
    """
    Upsert a feedback example. label: 'dimension' | 'ignore'
    Repeated confirms bump count (stronger learning signal).
    """
    label = label.strip().lower()
    if label not in {"dimension", "ignore"}:
        raise ValueError("label must be 'dimension' or 'ignore'")
    nt = _norm(text)
    if not nt:
        raise ValueError("empty text")

    examples = load_feedback()
    matched = None
    for ex in examples:
        if _norm(ex.get("text", "")) != nt:
            continue
        if ex.get("label") != label:
            continue
        # Same-ish location (or no coords on either side)
        ex_x, ex_y = ex.get("x"), ex.get("y")
        if x is not None and y is not None and ex_x is not None and ex_y is not None:
            if math.hypot(float(ex_x) - float(x), float(ex_y) - float(y)) > 40:
                continue
        matched = ex
        break

    if matched is None:
        matched = {
            "text": text.strip(),
            "norm": nt,
            "label": label,
            "page": page,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "neighbors": neighbors or "",
            "source": source,
            "drawing_hint": drawing_hint or "",
            "count": 1,
            "created_at": _now(),
            "updated_at": _now(),
        }
        examples.append(matched)
    else:
        matched["count"] = int(matched.get("count") or 1) + 1
        matched["updated_at"] = _now()
        matched["source"] = source
        if neighbors:
            matched["neighbors"] = neighbors
        if page is not None:
            matched["page"] = page
        if x is not None:
            matched["x"] = x
        if y is not None:
            matched["y"] = y
        if width is not None:
            matched["width"] = width
        if height is not None:
            matched["height"] = height

    save_feedback(examples)
    return matched


def match_learned(
    text: str,
    *,
    page: int | None = None,
    x: float | None = None,
    y: float | None = None,
    min_count: int = 1,
) -> str | None:
    """
    Return 'dimension' / 'ignore' if a learned example matches, else None.
    Position is soft: same text with enough confirms matches globally;
    with coords, prefer nearby examples.
    """
    nt = _norm(text)
    if not nt:
        return None
    best: tuple[float, dict] | None = None
    for ex in load_feedback():
        if _norm(ex.get("text", "")) != nt:
            continue
        if int(ex.get("count") or 1) < min_count:
            continue
        score = float(ex.get("count") or 1)
        ex_x, ex_y = ex.get("x"), ex.get("y")
        if x is not None and y is not None and ex_x is not None and ex_y is not None:
            dist = math.hypot(float(ex_x) - float(x), float(ex_y) - float(y))
            if dist < 28:
                score += 50 - dist
            elif dist < 80:
                score += 10
            elif int(ex.get("count") or 1) < 2:
                # Weak single example far away — skip
                continue
        if page is not None and ex.get("page") is not None and int(ex["page"]) != int(page):
            score -= 2
        if best is None or score > best[0]:
            best = (score, ex)
    if best is None:
        return None
    return str(best[1].get("label") or "")


def few_shot_examples(limit: int = 16) -> list[dict[str, str]]:
    """Recent / strongest examples for AI prompts."""
    examples = load_feedback()
    examples.sort(
        key=lambda e: (int(e.get("count") or 1), e.get("updated_at") or ""),
        reverse=True,
    )
    out: list[dict[str, str]] = []
    for ex in examples[: max(1, limit)]:
        out.append({
            "text": str(ex.get("text") or ""),
            "label": str(ex.get("label") or ""),
            "neighbors": str(ex.get("neighbors") or ""),
        })
    return out


def feedback_counts() -> dict[str, int]:
    examples = load_feedback()
    dims = sum(1 for e in examples if e.get("label") == "dimension")
    ign = sum(1 for e in examples if e.get("label") == "ignore")
    return {"total": len(examples), "dimension": dims, "ignore": ign}
