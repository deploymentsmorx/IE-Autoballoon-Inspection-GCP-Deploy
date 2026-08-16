from __future__ import annotations

import json
import secrets
import shutil
import zipfile
from pathlib import Path

import fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.api.dependencies import require_perm
from app.core.config import JOBS, UPLOADS
from app.schemas import BalloonUpdate, ReportPayload
from app.services.report_pdf import (
    build_report_pdf,
    characteristics_to_report_rows,
    default_measure_columns,
    measure_status,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_dir(job_id: str) -> Path:
    d = JOBS / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _apply_zone_refs(src: Path, balloons: list[dict]) -> dict[int, str]:
    try:
        from app.services.border_grid import zone_references_for_balloons

        return zone_references_for_balloons(src, balloons)
    except Exception:
        return {}


def _build_report_rows(src: Path | None, balloons: list[dict]) -> list[dict]:
    zone_map = _apply_zone_refs(src, balloons) if src and src.exists() else {}
    return characteristics_to_report_rows(balloons, zone_map)


def _ensure_report_date(meta: dict) -> None:
    from datetime import datetime

    if not str(meta.get("report_date") or "").strip():
        meta["report_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")


def _ensure_measure_columns(meta: dict) -> None:
    cols = meta.get("measure_columns")
    if not cols:
        meta["measure_columns"] = default_measure_columns()


def _load_meta(job_id: str) -> dict:
    p = _job_dir(job_id) / "meta.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_meta(job_id: str, meta: dict) -> None:
    jd = _job_dir(job_id)
    (jd / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # Mirror metadata → Neon; PDFs → S3 (or local) when configured
    try:
        from app import job_store

        meta.setdefault("id", job_id)
        job_store.sync_job_files_to_storage(job_id, jd, meta)
        (jd / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        # Never fail the API if Neon/S3 sync has a transient error
        try:
            from app.job_store import upsert_job_from_meta

            upsert_job_from_meta(meta)
        except Exception:
            pass


def _renumber(balloons: list[dict], *, compact: bool = False) -> list[dict]:
    """
    Normalize balloon numbers.

    compact=False (default): keep existing numbers; assign max+1 for missing/dupes.
      Used on save/create so new balloons append as N+1 and edits do not reshuffle.
    compact=True: reassign 1..N in current balloon-number order (delete path).
    """
    if compact:
        ordered = sorted(balloons, key=lambda x: int(x.get("balloon") or 0))
        out = []
        for i, b in enumerate(ordered, start=1):
            nb = dict(b)
            nb["balloon"] = i
            out.append(nb)
        return out

    used: set[int] = set()
    out: list[dict] = []
    pending: list[dict] = []
    for b in balloons:
        nb = dict(b)
        n = int(nb.get("balloon") or 0)
        if n > 0 and n not in used:
            used.add(n)
            out.append(nb)
        else:
            pending.append(nb)
    next_no = (max(used) + 1) if used else 1
    for nb in pending:
        while next_no in used:
            next_no += 1
        nb["balloon"] = next_no
        used.add(next_no)
        next_no += 1
        out.append(nb)
    out.sort(key=lambda x: int(x.get("balloon") or 0))
    return out


def _next_balloon_no(balloons: list[dict]) -> int:
    nums = [int(b.get("balloon") or 0) for b in balloons]
    return (max(nums) + 1) if nums else 1


def _scrape_dim_text(src: Path, page: int, x: float, y: float, width: float, height: float) -> str:
    """Best-effort dimension text under the drawn box (no UI prompt)."""
    try:
        doc = fitz.open(src)
        try:
            pi = max(0, int(page) - 1)
            if pi >= len(doc):
                return ""
            p = doc[pi]
            pad = 2.0
            clip = fitz.Rect(x - pad, y - pad, x + width + pad, y + height + pad)
            raw = (p.get_text("text", clip=clip) or "").strip()
            return " ".join(raw.split())[:120]
        finally:
            doc.close()
    except Exception:
        return ""


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


def _page_geometry(pdf_path: Path, page_index: int = 0, scale: float = 1.35) -> dict:
    """
    Geometry for aligning balloon PDF coords (unrotated mediabox) with preview PNG
    (rotated page.rect space × preview scale).
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[min(max(0, page_index), len(doc) - 1)]
        w, h = float(page.rect.width), float(page.rect.height)
        s = float(scale)
        if max(w, h) * s > 4200:
            s = 4200.0 / max(w, h)
        mb = page.mediabox
        rm = page.rotation_matrix
        dm = page.derotation_matrix
        return {
            "rotation": int(page.rotation or 0),
            "page_width": w,
            "page_height": h,
            "mediabox_width": float(mb.width),
            "mediabox_height": float(mb.height),
            "preview_scale": s,
            # unrotated → display (page.rect space)
            "rot": [float(rm.a), float(rm.b), float(rm.c), float(rm.d), float(rm.e), float(rm.f)],
            # display → unrotated
            "derot": [float(dm.a), float(dm.b), float(dm.c), float(dm.d), float(dm.e), float(dm.f)],
        }
    finally:
        doc.close()


def _attach_page_geometry(meta: dict, job_id: str, page_index: int = 0) -> dict:
    try:
        jd = _job_dir(job_id)
        idx = int(meta.get("active_file") or 0)
        files = meta.get("files") or []
        if not files or idx >= len(files):
            return meta
        src = jd / files[idx]["path"]
        if not src.exists():
            return meta
        meta["page_geometry"] = _page_geometry(src, page_index)
    except Exception:
        pass
    return meta


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
    from app.services.autoballoon_engine import _resolve_tessdata

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

def _draw_balloons_overlay(src_pdf: Path, balloons: list[dict], out_pdf: Path) -> list[dict]:
    """Draw balloons onto a copy of the source PDF; return balloons with bx/by filled."""
    from app.services.autoballoon_engine import Characteristic, draw_balloons

    doc = fitz.open(src_pdf)
    try:
        chars = []
        for b in balloons:
            bx = b.get("bx")
            by = b.get("by")
            chars.append(
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
                    bx=float(bx) if bx is not None else None,
                    by=float(by) if by is not None else None,
                )
            )
        out = draw_balloons(doc, chars)
        try:
            out.save(str(out_pdf), deflate=True, garbage=3)
        finally:
            out.close()
        # Persist circle centres so the UI overlay matches the PDF leaders
        updated: list[dict] = []
        for b, c in zip(balloons, chars):
            nb = dict(b)
            nb["bx"] = c.bx
            nb["by"] = c.by
            updated.append(nb)
        return updated
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
        "material": "",
        "mass": "",
        "finish": "",
        "serial_number": "",
        "report_date": "",
        "company_name": "",
        "supplier_name": "",
        "customer_name": "",
        "authorized_signatory": "",
    }
    _ensure_report_date(meta)
    # Title block → report header fields (Part Number, Revision, Mass, etc.)
    if saved and saved[0].get("kind") == "pdf":
        from app.services.title_block_extract import extract_title_block_meta, merge_title_meta

        merge_title_meta(meta, extract_title_block_meta(jd / saved[0]["path"]))
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
    passes: int = 2,
    use_zones: bool = True,
    use_tiles: bool = True,
    user: dict = Depends(require_perm("write")),
):
    from app.services.autoballoon_engine import balloon_pdf
    import os

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    files = meta.get("files") or []
    if file_index < 0 or file_index >= len(files):
        raise HTTPException(status_code=400, detail="Invalid file index")
    src = jd / files[file_index]["path"]
    # Image / scanned drawings: try OCR so dimension text is detectable
    needs_ocr = files[file_index].get("kind") == "image" or not _pdf_has_text(src)
    if needs_ocr:
        _ocr_enrich_pdf(src)
    pdf_bytes = src.read_bytes()
    ai_on = os.environ.get("SMORX_AI_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
    # Multi-page PDF: detect + draw on every page; output stays one combined file
    page_count = _pdf_page_count(src)
    # Default 2 passes; skip dimension OCR when the PDF already has text (much faster).
    out_pdf, chars, pass_log = balloon_pdf(
        pdf_bytes,
        passes=max(1, min(passes, 5)),
        use_zones=use_zones,
        use_tiles=use_tiles,
        use_learning=True,
        ai_fallback=ai_on,
        use_ocr=needs_ocr,
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
            "bx": c.bx,
            "by": c.by,
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
    meta["report_rows"] = _build_report_rows(src, balloons)
    _ensure_measure_columns(meta)
    meta["ballooned_pdf"] = ballooned_path.name
    # Title extract can OCR-hang; never block balloon completion on it.
    try:
        from app.services.title_block_extract import extract_title_block_meta, merge_title_meta

        merge_title_meta(meta, extract_title_block_meta(src))
    except Exception:
        pass
    _ensure_report_date(meta)
    _save_meta(job_id, meta)
    return {
        "job_id": job_id,
        "count": len(balloons),
        "page_count": page_count,
        "pass_log": pass_log,
        "balloons": balloons,
    }


def _balloons_to_chars(balloons: list[dict]):
    from app.services.autoballoon_engine import Characteristic

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
            bx=float(b["bx"]) if b.get("bx") is not None else None,
            by=float(b["by"]) if b.get("by") is not None else None,
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
            "bx": c.bx,
            "by": c.by,
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
    from app.services.autoballoon_engine import (
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
            "balloon": _next_balloon_no(balloons),
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
def get_job(
    job_id: str,
    page: int = 0,
    user: dict = Depends(require_perm("read")),
):
    meta = _load_meta(job_id)
    # Always expose report rows when balloons exist (UI / PDF need them)
    if not meta.get("report_rows") and meta.get("balloons"):
        jd = _job_dir(job_id)
        idx = int(meta.get("active_file") or 0)
        files = meta.get("files") or []
        src = jd / files[idx]["path"] if files else None
        meta["report_rows"] = _build_report_rows(src, meta["balloons"]) if src else characteristics_to_report_rows(meta["balloons"])
        _save_meta(job_id, meta)
    _ensure_measure_columns(meta)
    _ensure_report_date(meta)
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
    return _attach_page_geometry(meta, job_id, max(0, int(page)))
def rebuild_report(job_id: str, user: dict = Depends(require_perm("write"))):
    """Rebuild report table from current balloons (keeps measured / instr values when possible)."""
    meta = _load_meta(job_id)
    balloons = meta.get("balloons") or []
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"] if files else None
    zone_map = _apply_zone_refs(src, balloons) if src else {}
    old_rows = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
    new_rows = []
    for i, b in enumerate(balloons, start=1):
        row = characteristics_to_report_rows([b], zone_map)[0]
        prev = old_rows.get(int(b.get("balloon") or i)) or {}
        row["sno"] = i
        row["instr_id"] = prev.get("instr_id", "")
        row["instrument"] = prev.get("instrument", "")
        row["unit"] = prev.get("unit") or row.get("unit") or "mm"
        row["measured"] = prev.get("measured") or ["", ""]
        row["remarks"] = prev.get("remarks", "")
        if prev.get("tol_low") not in (None, ""):
            row["tol_low"] = prev.get("tol_low")
        if prev.get("tol_high") not in (None, ""):
            row["tol_high"] = prev.get("tol_high")
        new_rows.append(row)
    meta["report_rows"] = new_rows
    _ensure_measure_columns(meta)
    _ensure_report_date(meta)
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
    files = meta.get("files") or []
    src_name = ""
    if 0 <= idx < len(files):
        src_name = str(files[idx].get("name") or "")
    stem = Path(src_name).stem if src_name else job_id
    out_name = f"{stem}_ballooned.pdf"
    return FileResponse(p, media_type="application/pdf", filename=out_name)


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
    # Preserve numbers (1,2,3…N+1). Do not reshuffle by page position.
    balloons = _renumber([b.model_dump() for b in body.balloons], compact=False)
    out_pdf = jd / f"ballooned_{idx}.pdf"
    balloons = _draw_balloons_overlay(src, balloons, out_pdf)
    _render_preview(out_pdf, jd / "balloon_preview.png", 0)
    # Keep report row alignment by balloon number; refresh zone reference from border grid
    old_rows = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
    zone_map = _apply_zone_refs(src, balloons)
    new_rows = []
    for i, b in enumerate(balloons, start=1):
        prev = old_rows.get(int(b["balloon"])) or old_rows.get(i) or {}
        row = characteristics_to_report_rows([b], zone_map)[0]
        row["sno"] = i
        row["instr_id"] = prev.get("instr_id", "")
        row["instrument"] = prev.get("instrument", "")
        row["unit"] = prev.get("unit") or row.get("unit") or "mm"
        row["measured"] = prev.get("measured") or ["", ""]
        row["remarks"] = prev.get("remarks", "")
        row["tol_low"] = prev.get("tol_low") or row.get("tol_low", "")
        row["tol_high"] = prev.get("tol_high") or row.get("tol_high", "")
        if prev.get("reference") and str(prev.get("reference")).startswith("P") is False:
            # Keep user-edited non-page references only if zone missing
            pass
        new_rows.append(row)
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    _ensure_measure_columns(meta)
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
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"] if files else None

    # Prefer text scraped from the drawn box; no UI prompt required
    cleaned = (text or "").strip()
    if not cleaned and src and src.exists():
        cleaned = _scrape_dim_text(src, page, x, y, width, height)

    balloons = list(meta.get("balloons") or [])
    next_no = _next_balloon_no(balloons)
    balloons.append({
        "balloon": next_no,
        "page": page,
        "text": cleaned,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "category": "manual",
        "pattern": "manual",
        # None → draw_balloons runs placement rules (leader + circle offset)
        "bx": None,
        "by": None,
    })
    # learn when we have useful text
    if cleaned:
        try:
            from app.services.learning_store import add_feedback
            add_feedback(text=cleaned, label="dimension", page=page, x=x, y=y, source="manual")
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
    # Compact 1..N after delete so the sequence stays contiguous
    balloons = _renumber(balloons, compact=True)
    body = BalloonUpdate(balloons=balloons)
    return update_balloons(job_id, body, user)


@router.post("/{job_id}/title-extract")
def extract_title_block(job_id: str, user: dict = Depends(require_perm("write"))):
    """Re-read title block / drawing labels into empty report header fields."""
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    if not files or idx >= len(files):
        raise HTTPException(status_code=404, detail="No drawing on this job")
    src = jd / files[idx]["path"]
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source drawing missing")
    from app.services.title_block_extract import extract_title_block_meta, merge_title_meta

    merge_title_meta(meta, extract_title_block_meta(src))
    _save_meta(job_id, meta)
    return {
        "part_number": meta.get("part_number", ""),
        "part_name": meta.get("part_name", ""),
        "revision": meta.get("revision", ""),
        "material": meta.get("material", ""),
        "mass": meta.get("mass", ""),
        "finish": meta.get("finish", ""),
        "serial_number": meta.get("serial_number", ""),
        "report_date": meta.get("report_date", ""),
        "company_name": meta.get("company_name", ""),
        "supplier_name": meta.get("supplier_name", ""),
        "customer_name": meta.get("customer_name", ""),
        "authorized_signatory": meta.get("authorized_signatory", ""),
    }


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
    meta["serial_number"] = body.serial_number
    meta["report_date"] = body.report_date
    meta["company_name"] = body.company_name
    meta["supplier_name"] = body.supplier_name
    meta["customer_name"] = body.customer_name
    meta["authorized_signatory"] = body.authorized_signatory
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
        jd = _job_dir(job_id)
        idx = int(meta.get("active_file") or 0)
        files = meta.get("files") or []
        src = jd / files[idx]["path"] if files else None
        rows = _build_report_rows(src, meta["balloons"]) if src else characteristics_to_report_rows(meta["balloons"])
        meta["report_rows"] = rows
        _save_meta(job_id, meta)
    _ensure_measure_columns(meta)
    _ensure_report_date(meta)
    payload = {
        "part_number": meta.get("part_number", ""),
        "part_name": meta.get("part_name", ""),
        "revision": meta.get("revision", ""),
        "material": meta.get("material", ""),
        "mass": meta.get("mass", ""),
        "finish": meta.get("finish", ""),
        "serial_number": meta.get("serial_number", ""),
        "report_date": meta.get("report_date", ""),
        "company_name": meta.get("company_name", ""),
        "supplier_name": meta.get("supplier_name", ""),
        "customer_name": meta.get("customer_name", ""),
        "authorized_signatory": meta.get("authorized_signatory", ""),
        "rows": rows,
        "measure_columns": meta.get("measure_columns") or default_measure_columns(),
    }
    try:
        pdf = build_report_pdf(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {e}") from e
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_inspection_report.pdf"'},
    )


@router.post("/{job_id}/report/pdf")
def save_and_download_report_pdf(
    job_id: str,
    body: ReportPayload,
    user: dict = Depends(require_perm("write")),
):
    """Save current report fields and return the PDF in one request (avoids a second GET that can hang)."""
    meta = _load_meta(job_id)
    meta["part_number"] = body.part_number
    meta["part_name"] = body.part_name
    meta["revision"] = body.revision
    meta["material"] = body.material
    meta["mass"] = body.mass
    meta["finish"] = body.finish
    meta["serial_number"] = body.serial_number
    meta["report_date"] = body.report_date
    meta["company_name"] = body.company_name
    meta["supplier_name"] = body.supplier_name
    meta["customer_name"] = body.customer_name
    meta["authorized_signatory"] = body.authorized_signatory
    meta["report_rows"] = [r.model_dump() for r in body.rows]
    meta["measure_columns"] = [c.model_dump() for c in body.measure_columns]
    _save_meta(job_id, meta)
    payload = body.model_dump()
    try:
        pdf = build_report_pdf(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {e}") from e
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
