"""Upload a scanned inspection sheet and suggest report fields."""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.dependencies import require_perm
from app.services.scan_report import apply_suggestion_to_meta, extract_inspection_report

router = APIRouter(prefix="/api/jobs", tags=["scans"])


def _store_scan(upload_name: str, raw_path: Path, dest_pdf: Path) -> None:
    from app.api.routes.autoballoon import _image_to_pdf, _is_image, _is_pdf

    if _is_pdf(upload_name):
        if raw_path.resolve() != dest_pdf.resolve():
            shutil.copy2(raw_path, dest_pdf)
        return
    if _is_image(upload_name):
        _image_to_pdf(raw_path, dest_pdf)
        return
    raise HTTPException(status_code=400, detail="Upload a PDF or image scan (png, jpg, tif, webp, bmp)")


@router.post("/scan-report")
async def scan_report(
    file: UploadFile = File(...),
    job_id: str = Form(""),
    user: dict = Depends(require_perm("write")),
):
    """
    Read a scanned inspection sheet. Results are stored as a suggestion only.
    The operator reviews them in the report and saves when they are correct.
    """
    from app.api.routes.autoballoon import (
        _copy_image_preview,
        _ensure_measure_columns,
        _ensure_report_date,
        _is_image,
        _is_pdf,
        _job_dir,
        _load_meta,
        _pdf_page_count,
        _render_preview,
        _save_meta,
    )

    name = Path(file.filename or "scan.pdf").name
    if not (_is_pdf(name) or _is_image(name)):
        raise HTTPException(
            status_code=400,
            detail="Upload a scanned PDF or image (png, jpg, jpeg, webp, tif, tiff, bmp)",
        )

    created = False
    if job_id.strip():
        meta = _load_meta(job_id.strip())
        jd = _job_dir(job_id.strip())
        job_id = job_id.strip()
    else:
        created = True
        job_id = secrets.token_hex(6)
        jd = _job_dir(job_id)
        meta = {
            "id": job_id,
            "owner": user["id"],
            "owner_username": user.get("username") or "",
            "files": [],
            "active_file": 0,
            "page_count": 1,
            "balloons": [],
            "status": "scanned",
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
            "report_rows": [],
        }

    raw_path = jd / f"scan_upload_{name}"
    with raw_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    pdf_path = jd / "scan_report.pdf"
    _store_scan(name, raw_path, pdf_path)

    try:
        suggestion = extract_inspection_report(pdf_path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read the scan: {exc}") from exc

    if created:
        page_count = 1
        try:
            page_count = _pdf_page_count(pdf_path)
        except Exception:
            page_count = 1
        combined_name = name if _is_pdf(name) else f"{Path(name).stem}.pdf"
        source_path = jd / f"source_0_{combined_name}"
        if source_path.resolve() != pdf_path.resolve():
            shutil.copy2(pdf_path, source_path)
        meta["files"] = [
            {
                "index": 0,
                "name": combined_name,
                "path": source_path.name,
                "kind": "image" if _is_image(name) else "pdf",
                "page_count": page_count,
                "source_names": [name],
            }
        ]
        meta["page_count"] = page_count
        if _is_image(name):
            _copy_image_preview(raw_path, jd / "input_preview.png")
        else:
            _render_preview(source_path, jd / "input_preview.png", 0)

    meta["scan_suggestion"] = suggestion
    meta["scan_file"] = name
    apply_suggestion_to_meta(meta, suggestion)
    _ensure_report_date(meta)
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta)
    return {
        "job_id": job_id,
        "created": created,
        "suggestion": suggestion,
        "message": suggestion.get("summary") or "Scan read. Review the report before saving.",
    }
