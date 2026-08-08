from __future__ import annotations

import json
import secrets
import shutil
import zipfile
from pathlib import Path

import fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from ..auth import require_perm
from ..db import JOBS, UPLOADS, BalloonUpdate, ReportPayload
from ..report_pdf import build_report_pdf, characteristics_to_report_rows, measure_status

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_dir(job_id: str) -> Path:
    d = JOBS / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_meta(job_id: str) -> dict:
    p = _job_dir(job_id) / "meta.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_meta(job_id: str, meta: dict) -> None:
    (_job_dir(job_id) / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _renumber(balloons: list[dict]) -> list[dict]:
    out = []
    for i, b in enumerate(sorted(balloons, key=lambda x: (x.get("page", 1), x.get("y", 0), x.get("x", 0))), start=1):
        nb = dict(b)
        nb["balloon"] = i
        out.append(nb)
    return out


def _render_preview(pdf_path: Path, out_png: Path, page_index: int = 0, scale: float = 1.35) -> None:
    """Write a page preview PNG atomically (avoids Windows file-lock races)."""
    png = _render_preview_bytes(pdf_path, page_index=page_index, scale=scale)
    tmp = out_png.with_suffix(f".{secrets.token_hex(4)}.tmp.png")
    try:
        tmp.write_bytes(png)
        tmp.replace(out_png)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _render_preview_bytes(pdf_path: Path, page_index: int = 0, scale: float = 1.35) -> bytes:
    """Render one PDF page to PNG bytes in memory (safe for concurrent preview requests)."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[min(max(0, page_index), len(doc) - 1)]
        # Cap scale on huge sheets so previews stay responsive
        w, h = float(page.rect.width), float(page.rect.height)
        s = float(scale)
        if max(w, h) * s > 4200:
            s = 4200.0 / max(w, h)
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _pdf_page_count(pdf_path: Path) -> int:
    doc = fitz.open(pdf_path)
    try:
        return max(1, len(doc))
    finally:
        doc.close()

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
_PDF_EXTS = {".pdf"}


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _is_pdf(name: str) -> bool:
    return Path(name).suffix.lower() in _PDF_EXTS


def _image_to_pdf(image_path: Path, pdf_path: Path) -> None:
    """Wrap a raster image as a single-page PDF (A3-ish page fitting the image)."""
    img_doc = fitz.open(image_path)
    try:
        # Convert image → PDF via PyMuPDF
        pdf_bytes = img_doc.convert_to_pdf()
    finally:
        img_doc.close()
    pdf = fitz.open("pdf", pdf_bytes)
    try:
        pdf.save(str(pdf_path), deflate=True)
    finally:
        pdf.close()


def _copy_image_preview(image_path: Path, out_png: Path, max_side: int = 2400) -> None:
    """Save an image as the input preview PNG (optionally downscaled)."""
    doc = fitz.open(image_path)
    try:
        page = doc[0]
        # page is image-sized; scale down if huge
        w, h = page.rect.width, page.rect.height
        scale = 1.0
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        pix.save(str(out_png))
    finally:
        doc.close()


def _pdf_has_text(pdf_path: Path, min_chars: int = 8) -> bool:
    try:
        doc = fitz.open(pdf_path)
        try:
            for page in doc:
                if len((page.get_text("text") or "").strip()) >= min_chars:
                    return True
        finally:
            doc.close()
    except Exception:
        return False
    return False


def _ocr_enrich_pdf(pdf_path: Path) -> bool:
    """
    Try to add OCR text layer so vector ballooning can see dimensions on scanned/image PDFs.
    Returns True if OCR text was applied. Requires Tesseract installed on the machine.
    """
    from ..autoballoon import _resolve_tessdata

    tessdata = _resolve_tessdata()
    if not tessdata:
        return False
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return False
    changed = False
    try:
        for page in doc:
            if len((page.get_text("text") or "").strip()) >= 8:
                continue
            try:
                tp = page.get_textpage_ocr(
                    dpi=200, full=True, language="eng", tessdata=tessdata,
                )
            except Exception:
                continue
            try:
                d = page.get_text("rawdict", textpage=tp)
                tw = fitz.TextWriter(page.rect)
                for block in d.get("blocks") or []:
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines") or []:
                        for span in line.get("spans") or []:
                            text = (span.get("text") or "").strip()
                            bbox = span.get("bbox")
                            if not text or not bbox:
                                continue
                            size = max(6.0, float(span.get("size") or 10))
                            tw.append(
                                fitz.Point(bbox[0], bbox[3] - 1),
                                text,
                                fontsize=size,
                                fontname="helv",
                            )
                            changed = True
                if changed:
                    # render_mode 3 = invisible text (selectable / detectable)
                    tw.write_text(page, render_mode=3)
            except Exception:
                continue
        if changed:
            tmp = pdf_path.with_suffix(".ocr.pdf")
            doc.save(str(tmp), deflate=True, garbage=3)
            doc.close()
            tmp.replace(pdf_path)
            return True
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return False

def _draw_balloons_overlay(src_pdf: Path, balloons: list[dict], out_pdf: Path) -> None:
    """Draw balloons onto a copy of the source PDF."""
    from ..autoballoon import Characteristic, draw_balloons

    doc = fitz.open(src_pdf)
    try:
        chars = [
            Characteristic(
                balloon=int(b["balloon"]),
                page=int(b["page"]),
                text=str(b.get("text") or ""),
                x=float(b["x"]),
                y=float(b["y"]),
                width=float(b.get("width") or 20),
                height=float(b.get("height") or 12),
                category=str(b.get("category") or ""),
                pattern=str(b.get("pattern") or ""),
                family="",
            )
            for b in balloons
        ]
        out = draw_balloons(doc, chars)
        try:
            out.save(str(out_pdf), deflate=True, garbage=3)
        finally:
            out.close()
    finally:
        doc.close()


@router.post("/upload")
async def upload_pdfs(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_perm("write")),
):
    accepted = [
        f
        for f in files
        if _is_pdf(f.filename or "") or _is_image(f.filename or "")
    ]
    if not accepted:
        raise HTTPException(
            status_code=400,
            detail="Upload one or more PDF or image files (png, jpg, jpeg, webp, tif, bmp)",
        )
    job_id = secrets.token_hex(6)
    jd = _job_dir(job_id)
    saved = []
    for i, f in enumerate(accepted):
        name = Path(f.filename or f"drawing_{i}").name
        raw_dest = jd / f"upload_{i}_{name}"
        with raw_dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)

        kind = "image" if _is_image(name) else "pdf"
        if kind == "image":
            pdf_name = f"source_{i}_{Path(name).stem}.pdf"
            pdf_dest = jd / pdf_name
            _image_to_pdf(raw_dest, pdf_dest)
            saved.append({
                "index": i,
                "name": name,
                "path": pdf_dest.name,
                "kind": "image",
                "image_path": raw_dest.name,
                "page_count": 1,
            })
        else:
            pdf_dest = jd / f"source_{i}_{name}"
            if raw_dest != pdf_dest:
                shutil.move(str(raw_dest), str(pdf_dest))
            pages = _pdf_page_count(pdf_dest)
            saved.append({
                "index": i,
                "name": name,
                "path": pdf_dest.name,
                "kind": "pdf",
                "page_count": pages,
            })

    page_count = int(saved[0].get("page_count") or 1) if saved else 1
    meta = {
        "id": job_id,
        "owner": user["id"],
        "files": saved,
        "active_file": 0,
        "page_count": page_count,
        "balloons": [],
        "status": "uploaded",
        "part_number": "",
        "part_name": "",
        "revision": "",
    }
    _save_meta(job_id, meta)
    # Input preview only (panel 1) — do NOT create balloon preview yet
    first = saved[0]
    if first.get("kind") == "image" and first.get("image_path"):
        _copy_image_preview(jd / first["image_path"], jd / "input_preview.png")
    else:
        _render_preview(jd / first["path"], jd / "input_preview.png", 0)
    # Ensure stale balloon preview is gone
    balloon_prev = jd / "balloon_preview.png"
    if balloon_prev.exists():
        balloon_prev.unlink()
    return {
        "job_id": job_id,
        "files": saved,
        "page_count": page_count,
        "message": (
            f"Uploaded multi-page PDF ({page_count} pages). "
            "Run auto ballooning to process every page; download stays one combined PDF."
            if page_count > 1
            else "Uploaded."
        ),
    }


@router.post("/{job_id}/balloon")
def run_balloon(
    job_id: str,
    file_index: int = 0,
    passes: int = 3,
    use_zones: bool = True,
    use_tiles: bool = True,
    user: dict = Depends(require_perm("write")),
):
    from ..autoballoon import balloon_pdf
    import os

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    files = meta.get("files") or []
    if file_index < 0 or file_index >= len(files):
        raise HTTPException(status_code=400, detail="Invalid file index")
    src = jd / files[file_index]["path"]
    # Image / scanned drawings: try OCR so dimension text is detectable
    if files[file_index].get("kind") == "image" or not _pdf_has_text(src):
        _ocr_enrich_pdf(src)
    pdf_bytes = src.read_bytes()
    ai_on = os.environ.get("SMORX_AI_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
    # Multi-page PDF: detect + draw on every page; output stays one combined file
    page_count = _pdf_page_count(src)
    out_pdf, chars, pass_log = balloon_pdf(
        pdf_bytes,
        passes=max(1, min(passes, 5)),
        use_zones=use_zones,
        use_tiles=use_tiles,
        use_learning=True,
        ai_fallback=ai_on,
    )
    balloons = [
        {
            "balloon": c.balloon,
            "page": c.page,
            "text": c.text,
            "x": c.x,
            "y": c.y,
            "width": c.width,
            "height": c.height,
            "category": c.category,
            "pattern": c.pattern,
            "bx": None,
            "by": None,
        }
        for c in chars
    ]
    ballooned_path = jd / f"ballooned_{file_index}.pdf"
    ballooned_path.write_bytes(out_pdf)
    _render_preview(ballooned_path, jd / "balloon_preview.png", 0)
    _render_preview(src, jd / "input_preview.png", 0)
    meta["active_file"] = file_index
    meta["page_count"] = page_count
    if files and file_index < len(files):
        files[file_index]["page_count"] = page_count
        meta["files"] = files
    meta["balloons"] = balloons
    meta["status"] = "ballooned"
    meta["pass_log"] = pass_log
    meta["report_rows"] = characteristics_to_report_rows(balloons)
    meta["measure_columns"] = []
    _save_meta(job_id, meta)
    return {
        "job_id": job_id,
        "count": len(balloons),
        "page_count": page_count,
        "pass_log": pass_log,
        "balloons": balloons,
    }


def _balloons_to_chars(balloons: list[dict]):
    from ..autoballoon import Characteristic

    return [
        Characteristic(
            balloon=int(b.get("balloon") or i + 1),
            page=int(b.get("page") or 1),
            text=str(b.get("text") or ""),
            x=float(b.get("x") or 0),
            y=float(b.get("y") or 0),
            width=float(b.get("width") or 20),
            height=float(b.get("height") or 12),
            category=str(b.get("category") or ""),
            pattern=str(b.get("pattern") or ""),
            family="",
        )
        for i, b in enumerate(balloons)
    ]


def _chars_to_balloons(chars) -> list[dict]:
    return [
        {
            "balloon": c.balloon,
            "page": c.page,
            "text": c.text,
            "x": c.x,
            "y": c.y,
            "width": c.width,
            "height": c.height,
            "category": c.category,
            "pattern": c.pattern,
            "bx": None,
            "by": None,
        }
        for c in chars
    ]


@router.post("/{job_id}/audit")
def audit_crosscheck(
    job_id: str,
    file_index: int = 0,
    rounds: int = 2,
    apply: bool = True,
    user: dict = Depends(require_perm("write")),
):
    """
    Audit + cross-check: recover missed dimensions, then list any still missing.
    apply=True merges recovered balloons into the job and redraws the preview.
    """
    from ..autoballoon import (
        balloon_pdf_crosscheck,
        find_unballooned_dimensions,
    )
    import fitz as _fitz

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    files = meta.get("files") or []
    if file_index < 0 or file_index >= len(files):
        raise HTTPException(status_code=400, detail="Invalid file index")
    src = jd / files[file_index]["path"]
    if files[file_index].get("kind") == "image" or not _pdf_has_text(src):
        _ocr_enrich_pdf(src)

    existing = _balloons_to_chars(meta.get("balloons") or [])
    before = len(existing)
    pdf_bytes = src.read_bytes()
    out_pdf, chars, pass_log = balloon_pdf_crosscheck(
        pdf_bytes,
        existing,
        rounds=max(1, min(rounds, 4)),
        start_level=2,
        use_zones=True,
        use_tiles=True,
        use_learning=True,
        ai_fallback=False,
    )
    added = max(0, len(chars) - before)
    balloons = _chars_to_balloons(chars)

    # Remaining misses after recovery
    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        missing = find_unballooned_dimensions(doc, chars, level=5, use_zones=True)
    finally:
        doc.close()

    if apply:
        ballooned_path = jd / f"ballooned_{file_index}.pdf"
        ballooned_path.write_bytes(out_pdf)
        _render_preview(ballooned_path, jd / "balloon_preview.png", 0)
        _render_preview(src, jd / "input_preview.png", 0)
        meta["active_file"] = file_index
        meta["balloons"] = balloons
        meta["status"] = "ballooned"
        meta["pass_log"] = (meta.get("pass_log") or []) + [
            {"mode": "audit_crosscheck", "added": added, "total": len(balloons), "missing": len(missing)}
        ]
        # Preserve measured values where balloon numbers still match text
        old_rows = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
        new_rows = characteristics_to_report_rows(balloons)
        for row in new_rows:
            prev = old_rows.get(int(row["balloon"])) or {}
            if prev.get("text") == row.get("nominal") or prev.get("nominal") == row.get("nominal"):
                row["instr_id"] = prev.get("instr_id", "")
                row["instrument"] = prev.get("instrument", "")
                row["measured"] = prev.get("measured") or ["", "", ""]
                row["remarks"] = prev.get("remarks", "")
        meta["report_rows"] = new_rows
        meta["missing_dimensions"] = missing
        _save_meta(job_id, meta)

    return {
        "job_id": job_id,
        "before": before,
        "added": added,
        "count": len(balloons),
        "missing_count": len(missing),
        "missing": missing,
        "pass_log": pass_log,
        "balloons": balloons if apply else None,
    }


@router.post("/{job_id}/audit/add-missing")
def add_missing_dimensions(
    job_id: str,
    body: dict,
    user: dict = Depends(require_perm("write")),
):
    """Add selected missing dimension candidates as balloons."""
    meta = _load_meta(job_id)
    items = body.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No items to add")
    balloons = list(meta.get("balloons") or [])
    for it in items:
        balloons.append({
            "balloon": len(balloons) + 1,
            "page": int(it.get("page") or 1),
            "text": str(it.get("text") or ""),
            "x": float(it.get("x") or 0),
            "y": float(it.get("y") or 0),
            "width": float(it.get("width") or 20),
            "height": float(it.get("height") or 12),
            "category": str(it.get("category") or "audit"),
            "pattern": str(it.get("pattern") or "audit_missing"),
            "bx": None,
            "by": None,
        })
    body_upd = BalloonUpdate(balloons=balloons)
    result = update_balloons(job_id, body_upd, user)
    # Refresh missing list after add
    meta2 = _load_meta(job_id)
    remaining = [
        m
        for m in (meta.get("missing_dimensions") or [])
        if not any(
            abs(float(m.get("x", 0)) - float(it.get("x", -1))) < 1
            and abs(float(m.get("y", 0)) - float(it.get("y", -1))) < 1
            for it in items
        )
    ]
    meta2["missing_dimensions"] = remaining
    _save_meta(job_id, meta2)
    result["missing_count"] = len(remaining)
    result["missing"] = remaining
    return result


@router.get("/{job_id}")
def get_job(job_id: str, user: dict = Depends(require_perm("read"))):
    meta = _load_meta(job_id)
    # Always expose report rows when balloons exist (UI / PDF need them)
    if not meta.get("report_rows") and meta.get("balloons"):
        meta["report_rows"] = characteristics_to_report_rows(meta["balloons"])
        _save_meta(job_id, meta)
    # Ensure page_count is available for multi-page UI
    if not meta.get("page_count"):
        files = meta.get("files") or []
        idx = int(meta.get("active_file") or 0)
        if files and 0 <= idx < len(files):
            p = _job_dir(job_id) / files[idx]["path"]
            if p.exists():
                try:
                    meta["page_count"] = _pdf_page_count(p)
                    _save_meta(job_id, meta)
                except Exception:
                    meta["page_count"] = 1
            else:
                meta["page_count"] = 1
        else:
            meta["page_count"] = 1
    return meta


@router.post("/{job_id}/report/rebuild")
def rebuild_report(job_id: str, user: dict = Depends(require_perm("write"))):
    """Rebuild report table from current balloons (keeps measured / instr values when possible)."""
    meta = _load_meta(job_id)
    balloons = meta.get("balloons") or []
    old_rows = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
    new_rows = []
    for i, b in enumerate(balloons, start=1):
        row = characteristics_to_report_rows([b])[0]
        prev = old_rows.get(int(b.get("balloon") or i)) or {}
        row["sno"] = i
        row["instr_id"] = prev.get("instr_id", "")
        row["instrument"] = prev.get("instrument", "")
        row["unit"] = prev.get("unit") or row.get("unit") or "mm"
        row["measured"] = prev.get("measured") or ["", "", ""]
        row["remarks"] = prev.get("remarks", "")
        if prev.get("tol_low") not in (None, ""):
            row["tol_low"] = prev.get("tol_low")
        if prev.get("tol_high") not in (None, ""):
            row["tol_high"] = prev.get("tol_high")
        new_rows.append(row)
    meta["report_rows"] = new_rows
    if "measure_columns" not in meta:
        meta["measure_columns"] = []
    _save_meta(job_id, meta)
    return {"count": len(new_rows), "rows": new_rows}

@router.get("/{job_id}/preview/input")
def preview_input(
    job_id: str,
    page: int = 0,
    user: dict = Depends(require_perm("read")),
):
    """Render one page of the source PDF (0-based). Multi-page PDFs stay combined on disk."""
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    if not files or idx >= len(files):
        raise HTTPException(status_code=404, detail="No preview")
    src = jd / files[idx]["path"]
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source PDF missing")
    page_i = max(0, int(page))
    # Images: serve cached PNG if present
    if files[idx].get("kind") == "image" and page_i == 0 and (jd / "input_preview.png").exists():
        return Response((jd / "input_preview.png").read_bytes(), media_type="image/png")
    try:
        png = _render_preview_bytes(src, page_i)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview render failed: {e}") from e
    if not png:
        raise HTTPException(status_code=500, detail="Preview render produced empty image")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{job_id}/preview/balloon")
def preview_balloon(
    job_id: str,
    page: int = 0,
    user: dict = Depends(require_perm("read")),
):
    """Render one page of the ballooned multi-page PDF (0-based)."""
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    ballooned = jd / f"ballooned_{idx}.pdf"
    if not ballooned.exists():
        raise HTTPException(status_code=404, detail="No preview — run auto ballooning first")
    page_i = max(0, int(page))
    try:
        png = _render_preview_bytes(ballooned, page_i)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview render failed: {e}") from e
    if not png:
        raise HTTPException(status_code=500, detail="Preview render produced empty image")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

@router.get("/{job_id}/download/ballooned")
def download_ballooned(job_id: str, user: dict = Depends(require_perm("read"))):
    meta = _load_meta(job_id)
    idx = int(meta.get("active_file") or 0)
    p = _job_dir(job_id) / f"ballooned_{idx}.pdf"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Ballooned PDF missing")
    return FileResponse(p, media_type="application/pdf", filename=f"{job_id}_ballooned.pdf")


@router.get("/{job_id}/download/source")
def download_source(job_id: str, user: dict = Depends(require_perm("read"))):
    meta = _load_meta(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    p = _job_dir(job_id) / files[idx]["path"]
    return FileResponse(p, media_type="application/pdf", filename=files[idx]["name"])


@router.put("/{job_id}/balloons")
def update_balloons(
    job_id: str,
    body: BalloonUpdate,
    user: dict = Depends(require_perm("write")),
):
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"]
    balloons = _renumber([b.model_dump() for b in body.balloons])
    out_pdf = jd / f"ballooned_{idx}.pdf"
    _draw_balloons_overlay(src, balloons, out_pdf)
    _render_preview(out_pdf, jd / "balloon_preview.png", 0)
    # Keep report row alignment by balloon number; refresh reference from page
    old_rows = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
    new_rows = []
    for i, b in enumerate(balloons, start=1):
        prev = old_rows.get(int(b["balloon"])) or old_rows.get(i) or {}
        row = characteristics_to_report_rows([b])[0]
        row["sno"] = i
        row["instr_id"] = prev.get("instr_id", "")
        row["instrument"] = prev.get("instrument", "")
        row["measured"] = prev.get("measured") or ["", "", ""]
        row["remarks"] = prev.get("remarks", "")
        row["tol_low"] = prev.get("tol_low") or row.get("tol_low", "")
        row["tol_high"] = prev.get("tol_high") or row.get("tol_high", "")
        row["reference"] = f"P{b['page']}"
        new_rows.append(row)
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    _save_meta(job_id, meta)
    return {"count": len(balloons), "balloons": balloons}


@router.post("/{job_id}/balloons/create")
def create_balloon(
    job_id: str,
    page: int = Form(1),
    text: str = Form(""),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(24),
    height: float = Form(14),
    user: dict = Depends(require_perm("write")),
):
    meta = _load_meta(job_id)
    balloons = list(meta.get("balloons") or [])
    balloons.append({
        "balloon": len(balloons) + 1,
        "page": page,
        "text": text or "DIM",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "category": "manual",
        "pattern": "manual",
    })
    # learn
    try:
        from ..learning_store import add_feedback
        add_feedback(text=text or "DIM", label="dimension", page=page, x=x, y=y, source="manual")
    except Exception:
        pass
    body = BalloonUpdate(balloons=balloons)
    return update_balloons(job_id, body, user)


@router.delete("/{job_id}/balloons/{balloon_no}")
def delete_balloon(
    job_id: str,
    balloon_no: int,
    user: dict = Depends(require_perm("write")),
):
    meta = _load_meta(job_id)
    balloons = [b for b in (meta.get("balloons") or []) if int(b.get("balloon")) != int(balloon_no)]
    body = BalloonUpdate(balloons=balloons)
    return update_balloons(job_id, body, user)


@router.put("/{job_id}/report")
def save_report(
    job_id: str,
    body: ReportPayload,
    user: dict = Depends(require_perm("write")),
):
    meta = _load_meta(job_id)
    meta["part_number"] = body.part_number
    meta["part_name"] = body.part_name
    meta["revision"] = body.revision
    meta["material"] = body.material
    meta["mass"] = body.mass
    meta["finish"] = body.finish
    meta["report_rows"] = [r.model_dump() for r in body.rows]
    meta["measure_columns"] = [c.model_dump() for c in body.measure_columns]
    _save_meta(job_id, meta)
    return {"ok": True}


@router.get("/{job_id}/report/status")
def report_cell_status(
    job_id: str,
    nominal: str,
    tol_low: str = "",
    tol_high: str = "",
    measured: str = "",
    user: dict = Depends(require_perm("read")),
):
    return {"status": measure_status(nominal, tol_low, tol_high, measured)}


@router.get("/{job_id}/report/pdf")
def download_report_pdf(job_id: str, user: dict = Depends(require_perm("read"))):
    meta = _load_meta(job_id)
    rows = meta.get("report_rows") or []
    if not rows and meta.get("balloons"):
        rows = characteristics_to_report_rows(meta["balloons"])
        meta["report_rows"] = rows
        _save_meta(job_id, meta)
    payload = {
        "part_number": meta.get("part_number", ""),
        "part_name": meta.get("part_name", ""),
        "revision": meta.get("revision", ""),
        "material": meta.get("material", ""),
        "mass": meta.get("mass", ""),
        "finish": meta.get("finish", ""),
        "rows": rows,
        "measure_columns": meta.get("measure_columns") or [],
    }
    pdf = build_report_pdf(payload)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_inspection_report.pdf"'},
    )


@router.get("/{job_id}/report/excel")
def download_excel(job_id: str, user: dict = Depends(require_perm("read"))):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    meta = _load_meta(job_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Inspection"
    headers = [
        "S.No", "Balloon", "Reference", "Nominal", "Tol low", "Tol high",
        "Instrument", "Unit", "Instr. ID", "Measured 1", "Measured 2", "Measured 3", "Remarks",
    ]
    extra = meta.get("measure_columns") or []
    for col in extra:
        headers.insert(-1, col.get("label") or "Measured")
    ws.append(headers)
    fills = {
        "green": PatternFill("solid", fgColor="C6EFCE"),
        "yellow": PatternFill("solid", fgColor="FFEB9C"),
        "red": PatternFill("solid", fgColor="FFC7CE"),
    }
    for row in meta.get("report_rows") or []:
        measured = list(row.get("measured") or ["", "", ""])
        while len(measured) < 3:
            measured.append("")
        extra_vals = []
        bkey = str(row.get("balloon"))
        for col in extra:
            extra_vals.append((col.get("values") or {}).get(bkey, ""))
        values = [
            row.get("sno"), row.get("balloon"), row.get("reference"), row.get("nominal"),
            row.get("tol_low"), row.get("tol_high"), row.get("instrument"), row.get("unit"),
            row.get("instr_id"), measured[0], measured[1], measured[2], *extra_vals, row.get("remarks"),
        ]
        ws.append(values)
        r_idx = ws.max_row
        measure_start = 10
        all_m = measured[:3] + extra_vals
        for i, mv in enumerate(all_m):
            st = measure_status(
                str(row.get("nominal") or ""),
                str(row.get("tol_low") or ""),
                str(row.get("tol_high") or ""),
                str(mv or ""),
            )
            if st in fills:
                ws.cell(r_idx, measure_start + i).fill = fills[st]
    out = _job_dir(job_id) / "inspection_report.xlsx"
    wb.save(out)
    return FileResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job_id}_inspection_report.xlsx",
    )
