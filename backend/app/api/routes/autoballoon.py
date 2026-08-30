from __future__ import annotations

import json
import secrets
import shutil
import zipfile
from pathlib import Path

import fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from app.api.dependencies import require_perm
from app.core.config import JOBS, UPLOADS
from app.schemas import BalloonDeleteMany, BalloonUpdate, AreaBalloonRequest, ReportPayload, ReportRowsChunk
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
        from app.services.border_grid import zone_references_for_balloons, _dim_reference_point

        # Backfill tx/ty from dim box when missing (older jobs / pre-draw saves)
        for b in balloons:
            if b.get("tx") is None or b.get("ty") is None:
                rx, ry = _dim_reference_point(b)
                b["tx"] = rx
                b["ty"] = ry
        return zone_references_for_balloons(src, balloons)
    except Exception:
        return {}


def _build_report_rows(src: Path | None, balloons: list[dict]) -> list[dict]:
    zone_map = _apply_zone_refs(src, balloons) if src and src.exists() else {}
    return characteristics_to_report_rows(balloons, zone_map)


def _report_filter_suffix(
    page: int | None,
    area_page: int | None,
    area_w: float | None,
    area_h: float | None,
) -> str:
    parts: list[str] = []
    if page:
        parts.append(f"p{page}")
    if area_page and area_w and area_h:
        parts.append("area")
    return ("_" + "_".join(parts)) if parts else ""


def _filtered_report_rows(
    meta: dict,
    jd: Path,
    *,
    page: int | None = None,
    area_page: int | None = None,
    area_x: float | None = None,
    area_y: float | None = None,
    area_w: float | None = None,
    area_h: float | None = None,
) -> list[dict]:
    from app.services.report_pdf import filter_report_rows

    rows = list(meta.get("report_rows") or [])
    if not rows and meta.get("balloons"):
        idx = int(meta.get("active_file") or 0)
        files = meta.get("files") or []
        src = jd / files[idx]["path"] if files else None
        rows = _build_report_rows(src, meta["balloons"]) if src else characteristics_to_report_rows(meta["balloons"])
    return filter_report_rows(
        rows,
        meta.get("balloons") or [],
        page=page,
        area_page=area_page,
        area_x=area_x,
        area_y=area_y,
        area_w=area_w,
        area_h=area_h,
    )


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


def _save_meta(job_id: str, meta: dict, *, sync_files: bool = True) -> None:
    jd = _job_dir(job_id)
    (jd / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta.setdefault("id", job_id)
    if sync_files:
        # Mirror metadata → Neon; PDFs → S3 (or local) when configured
        try:
            from app import job_store

            job_store.sync_job_files_to_storage(job_id, jd, meta)
            (jd / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            try:
                from app.job_store import upsert_job_from_meta

                upsert_job_from_meta(meta)
            except Exception:
                pass
    else:
        # Fast path: metadata only (balloon edits without PDF regen)
        try:
            from app.job_store import upsert_job_from_meta

            upsert_job_from_meta(meta)
        except Exception:
            pass


def _save_meta_local(job_id: str, meta: dict) -> None:
    """Write meta.json only — skip S3/Neon (used for large report PDF path)."""
    jd = _job_dir(job_id)
    (jd / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

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


def _normalize_incoming_balloons(
    balloons: list[dict],
    mode: str,
    *,
    compact: bool = False,
) -> list[dict]:
    """
    Per-page numbering allows duplicate balloon #1 on each page.
    Continuous numbering uses one global sequence.
    """
    if mode != "per_page":
        return _renumber(balloons, compact=compact)
    from collections import defaultdict

    by_page: dict[int, list[dict]] = defaultdict(list)
    for b in balloons:
        by_page[int(b.get("page") or 1)].append(dict(b))
    out: list[dict] = []
    for page in sorted(by_page):
        out.extend(_renumber(by_page[page], compact=compact))
    out.sort(key=lambda x: (int(x.get("page") or 1), int(x.get("balloon") or 0)))
    return out


def _assert_unique_balloon_numbers(balloons: list[dict], mode: str) -> None:
    if mode == "per_page":
        from collections import defaultdict

        by_page: dict[int, set[int]] = defaultdict(set)
        for b in balloons:
            n = int(b.get("balloon") or 0)
            p = int(b.get("page") or 1)
            if n in by_page[p]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate balloon #{n} on page {p}. Edit numbers on that page, then Save again.",
                )
            by_page[p].add(n)
        return
    seen: set[int] = set()
    for b in balloons:
        n = int(b.get("balloon") or 0)
        if n in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate balloon #{n} in drawing. Swap numbers or pick a free number, then Save.",
            )
        seen.add(n)


def _balloon_fingerprint(b: dict) -> tuple:
    return (
        int(b.get("page") or 1),
        round(float(b.get("x") or 0), 1),
        round(float(b.get("y") or 0), 1),
        round(float(b.get("width") or 0), 1),
        round(float(b.get("height") or 0), 1),
        str(b.get("text") or "")[:80],
    )


def _balloon_numbers_changed(prev_balloons: list[dict], balloons: list[dict]) -> bool:
    if len(prev_balloons) != len(balloons):
        return False
    prev_map = {_balloon_fingerprint(b): int(b.get("balloon") or 0) for b in prev_balloons}
    for b in balloons:
        fp = _balloon_fingerprint(b)
        if fp not in prev_map:
            return True
        if prev_map[fp] != int(b.get("balloon") or 0):
            return True
    return False


def _rows_for_balloon_numbers(
    balloons: list[dict],
    prev_balloons: list[dict],
    old_rows: list[dict],
    zone_map: dict[int, str] | None = None,
) -> list[dict]:
    """Rebuild inspection rows when balloon numbers change or sequence resets."""
    zone_map = zone_map or {}
    old_map = {int(r.get("balloon")): dict(r) for r in old_rows}
    fp_to_row: dict[tuple, dict] = {}
    for b in prev_balloons:
        bno = int(b.get("balloon") or 0)
        if bno in old_map:
            fp_to_row[_balloon_fingerprint(b)] = dict(old_map[bno])

    ordered = sorted(balloons, key=lambda x: int(x.get("balloon") or 0))
    new_rows: list[dict] = []
    for i, b in enumerate(ordered, start=1):
        bno = int(b.get("balloon") or i)
        fp = _balloon_fingerprint(b)
        if fp in fp_to_row:
            row = dict(fp_to_row[fp])
        else:
            row = characteristics_to_report_rows([b], zone_map)[0]
        row["sno"] = i
        row["balloon"] = bno
        row["page"] = int(b.get("page") or 1)
        text = str(b.get("text") or "")
        if text:
            fresh = characteristics_to_report_rows([b], zone_map)[0]
            row["nominal"] = fresh.get("nominal", row.get("nominal", ""))
            row["tol_low"] = fresh.get("tol_low", row.get("tol_low", ""))
            row["tol_high"] = fresh.get("tol_high", row.get("tol_high", ""))
        zone_ref = zone_map.get(bno)
        if zone_ref:
            row["reference"] = zone_ref
        elif not row.get("reference") or str(row.get("reference", "")).startswith("P"):
            row["reference"] = f"P{b.get('page', 1)}"
        new_rows.append(row)
    return new_rows


def _next_balloon_no(balloons: list[dict]) -> int:
    nums = [int(b.get("balloon") or 0) for b in balloons]
    return (max(nums) + 1) if nums else 1


def _numbering_mode(meta: dict) -> str:
    mode = str(meta.get("balloon_numbering") or "continuous").strip().lower()
    return "per_page" if mode == "per_page" else "continuous"


def _renumber_job_balloons(balloons: list[dict], mode: str) -> list[dict]:
    if mode == "per_page":
        from collections import defaultdict

        by_page: dict[int, list[dict]] = defaultdict(list)
        for b in balloons:
            by_page[int(b.get("page") or 1)].append(dict(b))
        out: list[dict] = []
        for page in sorted(by_page):
            ordered = sorted(
                by_page[page],
                key=lambda b: (float(b.get("y") or 0), float(b.get("x") or 0)),
            )
            for i, b in enumerate(ordered, start=1):
                b["balloon"] = i
                out.append(b)
        return out
    return _renumber(balloons, compact=True)


def _balloon_in_rect(b: dict, rect) -> bool:
    import fitz

    cx = float(b.get("x") or 0) + float(b.get("width") or 0) / 2.0
    cy = float(b.get("y") or 0) + float(b.get("height") or 0) / 2.0
    return bool(rect.contains(fitz.Point(cx, cy)))


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


def _input_preview_cache_path(jd: Path, page_index: int) -> Path:
    return jd / f"input_preview_p{page_index}.png"


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


def _merge_pdfs(pdf_paths: list[Path], out_path: Path) -> int:
    """Combine multiple PDFs into one file; return total page count."""
    if not pdf_paths:
        raise ValueError("No PDFs to merge")
    if len(pdf_paths) == 1:
        if pdf_paths[0].resolve() != out_path.resolve():
            shutil.copy2(pdf_paths[0], out_path)
        return _pdf_page_count(out_path)
    merged = fitz.open()
    try:
        for p in pdf_paths:
            src = fitz.open(p)
            try:
                merged.insert_pdf(src)
            finally:
                src.close()
        merged.save(str(out_path), deflate=True, garbage=3)
        return max(1, len(merged))
    finally:
        merged.close()


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

def _default_balloon_leader(x: float, y: float, width: float, height: float) -> tuple[float, float]:
    """Fast leader placement — matches frontend BalloonCanvas fallback."""
    tip_x = x + width / 2.0
    tip_y = y + height / 2.0
    offset = max(28.0, height * 3.2)
    return tip_x - offset, tip_y


def _ensure_balloon_leaders(balloons: list[dict]) -> list[dict]:
    """Fill missing bx/by without opening the full PDF overlay pass."""
    out: list[dict] = []
    for b in balloons:
        nb = dict(b)
        if nb.get("bx") is None or nb.get("by") is None:
            bx, by = _default_balloon_leader(
                float(nb["x"]),
                float(nb["y"]),
                float(nb.get("width") or 20),
                float(nb.get("height") or 12),
            )
            nb["bx"] = bx
            nb["by"] = by
        out.append(nb)
    return out


def _regenerate_balloon_pdf(job_id: str, meta: dict) -> list[dict]:
    """Redraw ballooned PDF from meta; returns updated balloons with refined bx/by."""
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"]
    balloons = list(meta.get("balloons") or [])
    out_pdf = jd / f"ballooned_{idx}.pdf"
    balloons = _draw_balloons_overlay(src, balloons, out_pdf)
    _render_preview(out_pdf, jd / "balloon_preview.png", 0)
    meta["balloons"] = balloons
    meta["balloon_pdf_stale"] = False
    _save_meta(job_id, meta)
    return balloons


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
                    tx=float(b["tx"]) if b.get("tx") is not None else None,
                    ty=float(b["ty"]) if b.get("ty") is not None else None,
                    arrow_size=float(b["arrow_size"]) if b.get("arrow_size") is not None else None,
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
            nb["tx"] = c.tx
            nb["ty"] = c.ty
            nb["arrow_size"] = c.arrow_size
            updated.append(nb)
        return updated
    finally:
        doc.close()


@router.post("/upload")
async def upload_pdfs(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_perm("write")),
):
    """
    Upload one or more PDF/image drawings.
    Multiple files are merged into a single combined PDF so ballooning
    and download cover every page in one job.
    """
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

    # Convert each upload to a PDF part, then merge into one source file.
    part_pdfs: list[Path] = []
    source_names: list[str] = []
    any_image = False
    first_image_path: str | None = None

    for i, f in enumerate(accepted):
        name = Path(f.filename or f"drawing_{i}").name
        source_names.append(name)
        raw_dest = jd / f"upload_{i}_{name}"
        with raw_dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)

        if _is_image(name):
            any_image = True
            part = jd / f"part_{i}_{Path(name).stem}.pdf"
            _image_to_pdf(raw_dest, part)
            if first_image_path is None:
                first_image_path = raw_dest.name
            part_pdfs.append(part)
        else:
            part = jd / f"part_{i}_{name}"
            if raw_dest != part:
                shutil.move(str(raw_dest), str(part))
            part_pdfs.append(part)

    if len(source_names) == 1 and _is_pdf(source_names[0]):
        combined_name = source_names[0]
    elif len(source_names) == 1:
        combined_name = f"{Path(source_names[0]).stem}.pdf"
    else:
        stem = Path(source_names[0]).stem
        combined_name = f"{stem}_plus_{len(source_names) - 1}_combined.pdf"

    combined_path = jd / f"source_0_{combined_name}"
    page_count = _merge_pdfs(part_pdfs, combined_path)

    # Drop intermediate part PDFs when they differ from the combined output
    for p in part_pdfs:
        try:
            if p.resolve() != combined_path.resolve() and p.exists():
                p.unlink()
        except OSError:
            pass

    saved = [{
        "index": 0,
        "name": combined_name,
        "path": combined_path.name,
        "kind": "image" if (len(accepted) == 1 and any_image) else "pdf",
        "page_count": page_count,
        "source_names": source_names,
        "combined": len(source_names) > 1,
    }]
    if first_image_path and len(accepted) == 1:
        saved[0]["image_path"] = first_image_path

    meta = {
        "id": job_id,
        "owner": user["id"],
        "owner_username": user.get("username") or "",
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
    try:
        from app.services.title_block_extract import extract_title_block_meta, merge_title_meta

        merge_title_meta(meta, extract_title_block_meta(combined_path))
    except Exception:
        pass
    _save_meta(job_id, meta)
    # Input preview only (panel 1) — do NOT create balloon preview yet
    if saved[0].get("kind") == "image" and saved[0].get("image_path"):
        _copy_image_preview(jd / saved[0]["image_path"], jd / "input_preview.png")
    else:
        _render_preview(combined_path, jd / "input_preview.png", 0)
    # Ensure stale balloon preview is gone
    balloon_prev = jd / "balloon_preview.png"
    if balloon_prev.exists():
        balloon_prev.unlink()

    n = len(source_names)
    if n > 1:
        msg = (
            f"Combined {n} files into one PDF ({page_count} pages). "
            "Run auto ballooning to process every page."
        )
    elif page_count > 1:
        msg = (
            f"Uploaded multi-page PDF ({page_count} pages). "
            "Run auto ballooning to process every page."
        )
    else:
        msg = "Uploaded."

    return {
        "job_id": job_id,
        "files": saved,
        "page_count": page_count,
        "message": msg,
    }


@router.post("/{job_id}/balloon")
def run_balloon(
    job_id: str,
    file_index: int = 0,
    passes: int = 1,
    use_zones: bool = True,
    use_tiles: bool = True,
    numbering_mode: str = Query("continuous", description="continuous | per_page"),
    user: dict = Depends(require_perm("write")),
):
    from app.services.autoballoon_engine import balloon_pdf
    import logging
    import os
    import time

    log = logging.getLogger("smorx.balloon")
    t0 = time.perf_counter()

    meta = _load_meta(job_id)
    mode = "per_page" if str(numbering_mode).strip().lower() == "per_page" else "continuous"
    meta["balloon_numbering"] = mode
    jd = _job_dir(job_id)
    files = meta.get("files") or []
    if file_index < 0 or file_index >= len(files):
        raise HTTPException(status_code=400, detail="Invalid file index")
    src = jd / files[file_index]["path"]
    fname = files[file_index].get("name") or src.name
    log.info("balloon start job=%s file=%s passes=%s mode=%s", job_id, fname, passes, mode)
    # Image / scanned drawings: try OCR so dimension text is detectable
    needs_ocr = files[file_index].get("kind") == "image" or not _pdf_has_text(src)
    if needs_ocr:
        t_ocr = time.perf_counter()
        _ocr_enrich_pdf(src)
        log.info("balloon ocr_enrich job=%s %.1fs", job_id, time.perf_counter() - t_ocr)
    pdf_bytes = src.read_bytes()
    ai_on = os.environ.get("SMORX_AI_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
    # Multi-page PDF: detect + draw on every page; output stays one combined file
    page_count = _pdf_page_count(src)
    # Default 1 pass; dense sheets skip slow tile sweeps automatically.
    t_detect = time.perf_counter()
    out_pdf, chars, pass_log = balloon_pdf(
        pdf_bytes,
        passes=max(1, min(passes, 5)),
        use_zones=use_zones,
        use_tiles=use_tiles,
        use_learning=True,
        ai_fallback=ai_on,
        use_ocr=needs_ocr,
        numbering_mode=mode,
    )
    log.info(
        "balloon detect job=%s %.1fs balloons=%d pass_log=%s",
        job_id,
        time.perf_counter() - t_detect,
        len(chars),
        pass_log,
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
            "tx": c.tx,
            "ty": c.ty,
            "arrow_size": c.arrow_size,
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
    log.info("balloon done job=%s total=%.1fs balloons=%d", job_id, time.perf_counter() - t0, len(balloons))
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
            tx=float(b["tx"]) if b.get("tx") is not None else None,
            ty=float(b["ty"]) if b.get("ty") is not None else None,
            arrow_size=float(b["arrow_size"]) if b.get("arrow_size") is not None else None,
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
            "tx": c.tx,
            "ty": c.ty,
            "arrow_size": c.arrow_size,
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
        zone_map = _apply_zone_refs(src, balloons) if src and src.exists() else {}
        new_rows = characteristics_to_report_rows(balloons, zone_map)
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


@router.post("/{job_id}/report/rebuild")
def rebuild_report(job_id: str, user: dict = Depends(require_perm("write"))):
    """Rebuild report table from current balloons (keeps measured / instr values when possible)."""
    meta = _load_meta(job_id)
    balloons = meta.get("balloons") or []
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"] if files else None
    zone_map = _apply_zone_refs(src, balloons) if src and src.exists() else {}
    meta["balloons"] = balloons  # persist tx/ty backfill from dim box
    old_rows = {_row_key(r): dict(r) for r in (meta.get("report_rows") or [])}
    new_rows = _report_rows_full(balloons, old_rows, zone_map)
    # Always apply fresh grid refs (do not keep stale P1 / wrong zones)
    for row in new_rows:
        bno = int(row.get("balloon") or 0)
        page = int(row.get("page") or 1)
        zone_ref = zone_map.get((page, bno)) or zone_map.get(bno)
        if zone_ref:
            row["reference"] = zone_ref
    meta["report_rows"] = new_rows
    _ensure_measure_columns(meta)
    _ensure_report_date(meta)
    _save_meta_local(job_id, meta)
    return {"count": len(new_rows), "rows": new_rows}

@router.get("/{job_id}/page-geometry")
def get_page_geometry(
    job_id: str,
    page: int = 0,
    user: dict = Depends(require_perm("read")),
):
    """Lightweight page alignment data for canvas overlay (no full job payload)."""
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    if not files or idx >= len(files):
        raise HTTPException(status_code=404, detail="Job not found")
    src = jd / files[idx]["path"]
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source PDF missing")
    page_i = max(0, int(page))
    try:
        return _page_geometry(src, page_i)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Page geometry failed: {e}") from e


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
    cache = _input_preview_cache_path(jd, page_i)
    src_mtime = src.stat().st_mtime
    # Images: serve cached PNG if present
    if files[idx].get("kind") == "image" and page_i == 0 and (jd / "input_preview.png").exists():
        return Response(
            (jd / "input_preview.png").read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    if cache.exists() and cache.stat().st_mtime >= src_mtime:
        return Response(
            cache.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    try:
        png = _render_preview_bytes(src, page_i)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview render failed: {e}") from e
    if not png:
        raise HTTPException(status_code=500, detail="Preview render produced empty image")
    try:
        _render_preview(src, cache, page_i)
    except Exception:
        pass
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


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
    if meta.get("balloon_pdf_stale"):
        _regenerate_balloon_pdf(job_id, meta)
        meta = _load_meta(job_id)
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


def _row_key(b_or_r: dict) -> tuple[int, int]:
    return (int(b_or_r.get("page") or 1), int(b_or_r.get("balloon") or 0))


def _merge_report_row(prev: dict, row: dict, b: dict, zone_map: dict) -> dict:
    bno = int(b.get("balloon") or row.get("balloon") or 0)
    page = int(b.get("page") or row.get("page") or 1)
    row = dict(row)
    row["page"] = page
    row["instr_id"] = prev.get("instr_id", "")
    row["instrument"] = prev.get("instrument", "")
    row["unit"] = prev.get("unit") or row.get("unit") or "mm"
    row["measured"] = prev.get("measured") or ["", ""]
    row["remarks"] = prev.get("remarks", "")
    row["tol_low"] = prev.get("tol_low") or row.get("tol_low", "")
    row["tol_high"] = prev.get("tol_high") or row.get("tol_high", "")
    zone_ref = zone_map.get((page, bno)) or zone_map.get(bno)
    if zone_ref:
        row["reference"] = zone_ref
    return row


def _report_rows_fast(
    balloons: list[dict],
    old_rows: dict,
    zone_map: dict | None = None,
    prev_balloons: list[dict] | None = None,
) -> list[dict]:
    """Update report rows; always refresh Reference from full zone_map when provided."""
    zone_map = zone_map or {}
    fp_to_row: dict[tuple, dict] = {}
    if prev_balloons:
        for b in prev_balloons:
            rk = _row_key(b)
            if rk in old_rows:
                fp_to_row[_balloon_fingerprint(b)] = dict(old_rows[rk])
    ordered = sorted(
        balloons,
        key=lambda x: (int(x.get("page") or 1), int(x.get("balloon") or 0)),
    )
    new_rows = []
    for i, b in enumerate(ordered, start=1):
        bno = int(b.get("balloon") or i)
        page = int(b.get("page") or 1)
        rk = (page, bno)
        fp = _balloon_fingerprint(b)
        prev = fp_to_row.get(fp) or old_rows.get(rk) or old_rows.get(bno) or {}
        if prev:
            row = dict(prev)
            row["sno"] = i
            row["balloon"] = bno
            row["page"] = page
        else:
            row = characteristics_to_report_rows([b], zone_map)[0]
            row["sno"] = i
        text = str(b.get("text") or "")
        if text:
            fresh = characteristics_to_report_rows([b], zone_map)[0]
            row["nominal"] = fresh.get("nominal", "")
            row["tol_low"] = fresh.get("tol_low", "")
            row["tol_high"] = fresh.get("tol_high", "")
        zone_ref = zone_map.get(rk) or zone_map.get(bno)
        if zone_ref:
            row["reference"] = zone_ref
        elif not row.get("reference") or str(row.get("reference", "")).startswith("P"):
            row["reference"] = f"P{page}"
        new_rows.append(row)
    return new_rows


def _report_rows_full(
    balloons: list[dict],
    old_rows: dict,
    zone_map: dict,
) -> list[dict]:
    ordered = sorted(
        balloons,
        key=lambda x: (int(x.get("page") or 1), int(x.get("balloon") or 0)),
    )
    base_rows = characteristics_to_report_rows(ordered, zone_map)
    new_rows = []
    for i, row in enumerate(base_rows, start=1):
        b = ordered[i - 1]
        rk = _row_key(b)
        prev = old_rows.get(rk) or old_rows.get(int(b.get("balloon"))) or old_rows.get(i) or {}
        row["sno"] = i
        new_rows.append(_merge_report_row(prev, row, b, zone_map))
    return new_rows


@router.put("/{job_id}/balloons")
def update_balloons(
    job_id: str,
    body: BalloonUpdate,
    user: dict = Depends(require_perm("write")),
    redraw_pdf: bool = Query(True),
):
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    src = jd / files[idx]["path"]
    # Preserve numbers (1,2,3…N+1). On delete-only saves, compact to 1..N.
    incoming = [b.model_dump() for b in body.balloons]
    prev_balloons = meta.get("balloons") or []
    compact_save = len(incoming) < len(prev_balloons)
    mode = _numbering_mode(meta)
    _assert_unique_balloon_numbers(incoming, mode)
    balloons = _normalize_incoming_balloons(incoming, mode, compact=compact_save)
    if redraw_pdf:
        out_pdf = jd / f"ballooned_{idx}.pdf"
        balloons = _draw_balloons_overlay(src, balloons, out_pdf)
        _render_preview(out_pdf, jd / "balloon_preview.png", 0)
        meta["balloon_pdf_stale"] = False
    else:
        balloons = _ensure_balloon_leaders(balloons)
        meta["balloon_pdf_stale"] = True
    prev_balloons = meta.get("balloons") or []
    old_rows = {_row_key(r): dict(r) for r in (meta.get("report_rows") or [])}
    numbers_changed = _balloon_numbers_changed(prev_balloons, balloons)
    prev_by_no = {int(b.get("balloon") or 0): b for b in prev_balloons}
    prev_nos = set(prev_by_no)
    added = [b for b in balloons if int(b.get("balloon") or 0) not in prev_nos]
    count_changed = len(balloons) != len(prev_balloons)

    def _pos_key(b: dict) -> tuple:
        return (
            round(float(b.get("x") or 0), 2),
            round(float(b.get("y") or 0), 2),
            round(float(b.get("width") or 0), 2),
            round(float(b.get("height") or 0), 2),
            int(b.get("page") or 1),
        )

    moved = [
        b
        for b in balloons
        if int(b.get("balloon") or 0) in prev_by_no
        and _pos_key(b) != _pos_key(prev_by_no[int(b.get("balloon") or 0)])
    ]
    zone_map = _apply_zone_refs(src, balloons) if src.exists() else {}
    if redraw_pdf or count_changed or numbers_changed or compact_save:
        new_rows = _rows_for_balloon_numbers(
            balloons,
            prev_balloons,
            list(meta.get("report_rows") or []),
            zone_map,
        )
    else:
        new_rows = _report_rows_fast(balloons, old_rows, zone_map, prev_balloons)
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=redraw_pdf)
    return {"count": len(balloons), "balloons": balloons, "report_rows": new_rows}


def _delete_balloons_from_meta(meta: dict, remove_nos: set[int]) -> tuple[list[dict], list[dict]]:
    """Remove balloons by number, compact 1..N, keep report row data aligned by index."""
    old_balloons = sorted(meta.get("balloons") or [], key=lambda x: int(x.get("balloon") or 0))
    old_rows = list(meta.get("report_rows") or [])
    if not old_balloons:
        raise HTTPException(status_code=404, detail="Balloon not found")
    paired: list[tuple[dict, dict | None]] = []
    for i, b in enumerate(old_balloons):
        row = old_rows[i] if i < len(old_rows) else None
        paired.append((b, row))
    kept = [(b, r) for b, r in paired if int(b.get("balloon") or 0) not in remove_nos]
    if len(kept) == len(old_balloons):
        raise HTTPException(status_code=404, detail="Balloon not found")
    kept_balloons = [dict(b) for b, _ in kept]
    kept_rows = [dict(r) for _, r in kept if r is not None]
    balloons = _renumber(kept_balloons, compact=True)
    if len(kept_rows) == len(balloons):
        new_rows: list[dict] = []
        for i, (b, row) in enumerate(zip(balloons, kept_rows), start=1):
            row["sno"] = i
            row["balloon"] = i
            new_rows.append(row)
    else:
        old_map = {int(r.get("balloon")): r for r in (meta.get("report_rows") or [])}
        new_rows = _report_rows_fast(balloons, old_map)
    return balloons, new_rows


@router.post("/{job_id}/balloons/delete-many")
def delete_balloons_many(
    job_id: str,
    body: BalloonDeleteMany,
    user: dict = Depends(require_perm("write")),
):
    """Fast delete for one or many balloons without uploading the full balloon list."""
    meta = _load_meta(job_id)
    remove = {int(n) for n in body.balloon_numbers if int(n) > 0}
    if not remove:
        raise HTTPException(status_code=400, detail="No balloon numbers to delete")
    balloons, new_rows = _delete_balloons_from_meta(meta, remove)
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    meta["balloon_pdf_stale"] = True
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {"count": len(balloons), "balloons": balloons, "report_rows": new_rows}


@router.delete("/{job_id}/balloons/{balloon_no}")
def delete_balloon(
    job_id: str,
    balloon_no: int,
    user: dict = Depends(require_perm("write")),
):
    meta = _load_meta(job_id)
    balloons, new_rows = _delete_balloons_from_meta(meta, {int(balloon_no)})
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    meta["balloon_pdf_stale"] = True
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {"count": len(balloons), "balloons": balloons, "report_rows": new_rows}


@router.post("/{job_id}/balloons/resequence")
def resequence_balloons(
    job_id: str,
    user: dict = Depends(require_perm("write")),
):
    """Renumber all balloons 1..N (by current number order) and sync the inspection report."""
    meta = _load_meta(job_id)
    prev = list(meta.get("balloons") or [])
    if not prev:
        return {"count": 0, "balloons": [], "report_rows": []}
    balloons = _renumber_job_balloons(prev, _numbering_mode(meta))
    new_rows = _rows_for_balloon_numbers(balloons, prev, list(meta.get("report_rows") or []))
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    meta["balloon_pdf_stale"] = True
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {"count": len(balloons), "balloons": balloons, "report_rows": new_rows}


@router.post("/{job_id}/balloons/delete-page")
def delete_page_balloons(
    job_id: str,
    page: int = Query(..., ge=1),
    user: dict = Depends(require_perm("write")),
):
    """Remove all balloons on one drawing page and renumber the rest."""
    meta = _load_meta(job_id)
    prev = list(meta.get("balloons") or [])
    kept = [b for b in prev if int(b.get("page") or 1) != int(page)]
    if len(kept) == len(prev):
        raise HTTPException(status_code=404, detail=f"No balloons on page {page}")
    balloons = _renumber_job_balloons(kept, _numbering_mode(meta))
    new_rows = _rows_for_balloon_numbers(balloons, prev, list(meta.get("report_rows") or []))
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    meta["balloon_pdf_stale"] = True
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {"count": len(balloons), "balloons": balloons, "report_rows": new_rows, "page": page}


@router.post("/{job_id}/balloon/area")
def balloon_area(
    job_id: str,
    body: AreaBalloonRequest,
    user: dict = Depends(require_perm("write")),
):
    """Detect dimensions inside a rectangle (optionally clear page / replace area balloons)."""
    import fitz
    from app.services.autoballoon_engine import detect_dimensions, draw_balloons, renumber_characteristics

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    idx = int(meta.get("active_file") or 0)
    files = meta.get("files") or []
    if not files or idx >= len(files):
        raise HTTPException(status_code=404, detail="No drawing on this job")
    src = jd / files[idx]["path"]
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source drawing missing")

    page = int(body.page)
    clip = fitz.Rect(body.x, body.y, body.x + body.width, body.y + body.height)
    prev_balloons = list(meta.get("balloons") or [])

    if body.clear_page:
        kept = [b for b in prev_balloons if int(b.get("page") or 1) != page]
    else:
        kept = list(prev_balloons)

    if body.replace_in_area:
        kept = [
            b for b in kept
            if int(b.get("page") or 1) != page or not _balloon_in_rect(b, clip)
        ]

    prior = _balloons_to_chars(kept)
    doc = fitz.open(str(src))
    try:
        new_chars = detect_dimensions(
            doc,
            level=1,
            already=prior,
            use_zones=True,
            clip=clip,
            page_indices=[page - 1],
        )
        merged = renumber_characteristics(
            prior + new_chars,
            mode=_numbering_mode(meta),
        )
        out_doc = draw_balloons(doc, merged)
        try:
            out_pdf = jd / f"ballooned_{idx}.pdf"
            out_doc.save(str(out_pdf), deflate=True, garbage=3)
            _render_preview(out_pdf, jd / "balloon_preview.png", 0)
        finally:
            out_doc.close()
    finally:
        doc.close()

    balloons = _chars_to_balloons(merged)
    zone_map = _apply_zone_refs(src, balloons) if src.exists() else {}
    new_rows = _rows_for_balloon_numbers(balloons, prev_balloons, list(meta.get("report_rows") or []), zone_map)
    meta["balloons"] = balloons
    meta["report_rows"] = new_rows
    meta["status"] = "ballooned"
    meta["balloon_pdf_stale"] = False
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {
        "count": len(balloons),
        "added": len(new_chars),
        "balloons": balloons,
        "report_rows": new_rows,
        "page": page,
    }


@router.post("/{job_id}/balloons/create")
def create_balloon(
    job_id: str,
    page: int = Form(1),
    text: str = Form(""),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(24),
    height: float = Form(14),
    balloon_no: int | None = Form(None),
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
    if balloon_no is not None and int(balloon_no) > 0:
        want = int(balloon_no)
        if any(int(b.get("balloon") or 0) == want for b in balloons):
            raise HTTPException(status_code=400, detail=f"Balloon #{want} already exists")
        next_no = want
    else:
        next_no = _next_balloon_no(balloons)
    bx, by = _default_balloon_leader(x, y, width, height)
    new_balloon = {
        "balloon": next_no,
        "page": page,
        "text": cleaned,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "category": "manual",
        "pattern": "manual",
        "bx": bx,
        "by": by,
        "tx": x + width / 2.0,
        "ty": y + height / 2.0,
    }
    prev = list(meta.get("balloons") or [])
    balloons = prev + [new_balloon]
    # learn when we have useful text
    if cleaned:
        try:
            from app.services.learning_store import add_feedback
            add_feedback(text=cleaned, label="dimension", page=page, x=x, y=y, source="manual")
        except Exception:
            pass
    zone_map = _apply_zone_refs(src, balloons) if src and src.exists() else {}
    rows = _rows_for_balloon_numbers(balloons, prev, list(meta.get("report_rows") or []), zone_map)
    meta["balloons"] = sorted(balloons, key=lambda b: int(b.get("balloon") or 0))
    meta["report_rows"] = rows
    meta["status"] = "ballooned"
    meta["balloon_pdf_stale"] = True
    _ensure_measure_columns(meta)
    _save_meta(job_id, meta, sync_files=False)
    return {"count": len(balloons), "balloons": balloons, "report_rows": rows}


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
    local_only: bool = Query(False),
    headers_only: bool = Query(False),
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
    if not headers_only:
        if body.rows:
            rows = [r.model_dump() for r in body.rows]
            jd = _job_dir(job_id)
            idx = int(meta.get("active_file") or 0)
            files = meta.get("files") or []
            src = jd / files[idx]["path"] if files else None
            balloons = list(meta.get("balloons") or [])
            if src and src.exists() and balloons:
                zone_map = _apply_zone_refs(src, balloons)
                meta["balloons"] = balloons
                for row in rows:
                    bno = int(row.get("balloon") or 0)
                    zone_ref = zone_map.get(bno)
                    if zone_ref:
                        row["reference"] = zone_ref
            meta["report_rows"] = rows
        if body.measure_columns:
            meta["measure_columns"] = [c.model_dump() for c in body.measure_columns]
    elif body.measure_columns:
        meta["measure_columns"] = [c.model_dump() for c in body.measure_columns]
    # Invalidate cached PDF when report data changes
    cached = _job_dir(job_id) / "inspection_report.pdf"
    if cached.exists():
        try:
            cached.unlink()
        except OSError:
            pass
    if local_only:
        _save_meta_local(job_id, meta)
    else:
        _save_meta(job_id, meta)
    return {"ok": True, "rows": len(meta.get("report_rows") or [])}


@router.put("/{job_id}/report/rows")
def save_report_rows_chunk(
    job_id: str,
    body: ReportRowsChunk,
    local_only: bool = Query(True),
    user: dict = Depends(require_perm("write")),
):
    """Upload report rows in chunks (keeps desktop UI responsive for 3000+ rows)."""
    meta = _load_meta(job_id)
    existing = list(meta.get("report_rows") or [])
    chunk = [r.model_dump() for r in body.rows]
    offset = max(0, int(body.offset))
    if body.replace and offset == 0:
        existing = chunk
    elif offset >= len(existing):
        existing.extend(chunk)
    else:
        end = offset + len(chunk)
        existing[offset:end] = chunk
        if end < len(existing) and body.done:
            existing = existing[:end]
    meta["report_rows"] = existing
    if body.measure_columns is not None:
        meta["measure_columns"] = [c.model_dump() for c in body.measure_columns]
    if body.done:
        cached = _job_dir(job_id) / "inspection_report.pdf"
        if cached.exists():
            try:
                cached.unlink()
            except OSError:
                pass
    if local_only:
        _save_meta_local(job_id, meta)
    else:
        _save_meta(job_id, meta)
    return {"ok": True, "rows": len(existing), "offset": offset}


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
def download_report_pdf(
    job_id: str,
    page: int | None = Query(None, ge=1),
    area_page: int | None = Query(None, ge=1),
    area_x: float | None = Query(None),
    area_y: float | None = Query(None),
    area_w: float | None = Query(None, gt=0),
    area_h: float | None = Query(None, gt=0),
    user: dict = Depends(require_perm("read")),
):
    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    suffix = _report_filter_suffix(page, area_page, area_w, area_h)
    out_pdf = jd / f"inspection_report{suffix}.pdf"
    meta_path = jd / "meta.json"
    has_filter = bool(page or (area_page and area_w and area_h))
    if (
        not has_filter
        and out_pdf.exists()
        and meta_path.exists()
        and out_pdf.stat().st_mtime >= meta_path.stat().st_mtime
        and out_pdf.stat().st_size > 1000
    ):
        return FileResponse(
            out_pdf,
            media_type="application/pdf",
            filename=f"{job_id}_inspection_report.pdf",
            headers={"Cache-Control": "no-store"},
        )
    rows = _filtered_report_rows(
        meta,
        jd,
        page=page,
        area_page=area_page,
        area_x=area_x,
        area_y=area_y,
        area_w=area_w,
        area_h=area_h,
    )
    if not has_filter and rows and not meta.get("report_rows"):
        meta["report_rows"] = rows
        _save_meta_local(job_id, meta)
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
        out_pdf.write_bytes(pdf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {e}") from e
    return FileResponse(
        out_pdf,
        media_type="application/pdf",
        filename=f"{job_id}_inspection_report{suffix}.pdf",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{job_id}/report/pdf")
def save_and_download_report_pdf(
    job_id: str,
    body: ReportPayload,
    user: dict = Depends(require_perm("write")),
):
    """Save current report fields (local) and return the PDF."""
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
    _save_meta_local(job_id, meta)
    payload = body.model_dump()
    jd = _job_dir(job_id)
    out_pdf = jd / "inspection_report.pdf"
    try:
        pdf = build_report_pdf(payload)
        out_pdf.write_bytes(pdf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {e}") from e
    return FileResponse(
        out_pdf,
        media_type="application/pdf",
        filename=f"{job_id}_inspection_report.pdf",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{job_id}/report/excel")
def download_excel(
    job_id: str,
    page: int | None = Query(None, ge=1),
    area_page: int | None = Query(None, ge=1),
    area_x: float | None = Query(None),
    area_y: float | None = Query(None),
    area_w: float | None = Query(None, gt=0),
    area_h: float | None = Query(None, gt=0),
    user: dict = Depends(require_perm("read")),
):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    suffix = _report_filter_suffix(page, area_page, area_w, area_h)
    rows = _filtered_report_rows(
        meta,
        jd,
        page=page,
        area_page=area_page,
        area_x=area_x,
        area_y=area_y,
        area_w=area_w,
        area_h=area_h,
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "Inspection"
    headers = [
        "S.No", "Page", "Balloon", "Reference", "Nominal", "Tol low", "Tol high",
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
    for row in rows:
        measured = list(row.get("measured") or ["", "", ""])
        while len(measured) < 3:
            measured.append("")
        extra_vals = []
        bkey = str(row.get("balloon"))
        for col in extra:
            extra_vals.append((col.get("values") or {}).get(bkey, ""))
        values = [
            row.get("sno"), row.get("page", 1), row.get("balloon"), row.get("reference"), row.get("nominal"),
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
    out = _job_dir(job_id) / f"inspection_report{suffix}.xlsx"
    wb.save(out)
    return FileResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job_id}_inspection_report{suffix}.xlsx",
    )


@router.get("/{job_id}/report/csv")
def download_report_csv(
    job_id: str,
    page: int | None = Query(None, ge=1),
    area_page: int | None = Query(None, ge=1),
    area_x: float | None = Query(None),
    area_y: float | None = Query(None),
    area_w: float | None = Query(None, gt=0),
    area_h: float | None = Query(None, gt=0),
    user: dict = Depends(require_perm("read")),
):
    from app.services.report_pdf import report_rows_to_csv

    meta = _load_meta(job_id)
    jd = _job_dir(job_id)
    suffix = _report_filter_suffix(page, area_page, area_w, area_h)
    rows = _filtered_report_rows(
        meta,
        jd,
        page=page,
        area_page=area_page,
        area_x=area_x,
        area_y=area_y,
        area_w=area_w,
        area_h=area_h,
    )
    data = report_rows_to_csv(rows, meta.get("measure_columns") or [])
    out = _job_dir(job_id) / f"inspection_report{suffix}.csv"
    out.write_bytes(data)
    return FileResponse(
        out,
        media_type="text/csv; charset=utf-8",
        filename=f"{job_id}_inspection_report{suffix}.csv",
    )


@router.get("/{job_id}/report/json")
def download_report_json(
    job_id: str,
    page: int | None = Query(None, ge=1),
    area_page: int | None = Query(None, ge=1),
    area_x: float | None = Query(None),
    area_y: float | None = Query(None),
    area_w: float | None = Query(None, gt=0),
    area_h: float | None = Query(None, gt=0),
    user: dict = Depends(require_perm("read")),
):
    from app.services.report_pdf import report_payload_to_json

    meta = _load_meta(job_id)
    suffix = _report_filter_suffix(page, area_page, area_w, area_h)
    data = report_payload_to_json(
        meta,
        page=page,
        area_page=area_page,
        area_x=area_x,
        area_y=area_y,
        area_w=area_w,
        area_h=area_h,
    )
    out = _job_dir(job_id) / f"inspection_report{suffix}.json"
    out.write_bytes(data)
    return FileResponse(
        out,
        media_type="application/json",
        filename=f"{job_id}_inspection_report{suffix}.json",
    )
