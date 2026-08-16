from __future__ import annotations

from app.services import autoballoon_service


def test_ocr_flag_callable():
    # May be True/False depending on Tesseract install
    assert isinstance(autoballoon_service.is_ocr_ready(), bool)
