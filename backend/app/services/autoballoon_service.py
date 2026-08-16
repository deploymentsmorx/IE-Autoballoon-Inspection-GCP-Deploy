"""AutoBalloon orchestration service (wraps vector/OCR engine)."""

from __future__ import annotations

from app.services.autoballoon_engine import balloon_pdf, ocr_available


def run_balloon(
    pdf_bytes: bytes,
    *,
    passes: int = 3,
    use_zones: bool = True,
    use_tiles: bool = True,
    use_learning: bool = True,
    ai_fallback: bool = False,
):
    return balloon_pdf(
        pdf_bytes,
        passes=passes,
        use_zones=use_zones,
        use_tiles=use_tiles,
        use_learning=use_learning,
        ai_fallback=ai_fallback,
    )


def is_ocr_ready() -> bool:
    return ocr_available()
