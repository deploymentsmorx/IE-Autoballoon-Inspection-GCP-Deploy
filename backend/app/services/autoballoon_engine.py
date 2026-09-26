"""
Auto-balloon dimensions on engineering drawing PDFs.

Detects dimension-like text from the vector PDF, numbers each one,
and draws balloon circles directly onto a new PDF (no raster / no augmentation).

Pattern registry covers ISO/IS + common ASME-style callouts mixed on Indian OEM drawings.
GD&T feature-control frames are out of scope for text regex (later: get_drawings()).
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz  # PyMuPDF

# Cached Tesseract tessdata dir (PyMuPDF OCR needs this on Windows).
_TESSDATA_DIR: str | None | bool = False  # False = unresolved; None = missing; str = path


def _resolve_tessdata() -> str | None:
    """Locate Tesseract eng.traineddata for PyMuPDF OCR fallback."""
    global _TESSDATA_DIR
    if _TESSDATA_DIR is not False:
        return _TESSDATA_DIR  # type: ignore[return-value]

    candidates: list[Path] = []
    env = (os.environ.get("TESSDATA_PREFIX") or os.environ.get("SMORX_TESSDATA") or "").strip()
    if env:
        candidates.append(Path(env))
        candidates.append(Path(env) / "tessdata")
    candidates.extend([
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tessdata"),
        Path("/usr/share/tesseract-ocr/5/tessdata"),
        Path("/usr/share/tesseract-ocr/4.00/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/opt/homebrew/share/tessdata"),
    ])
    for c in candidates:
        try:
            if (c / "eng.traineddata").is_file():
                resolved = str(c.resolve())
                _TESSDATA_DIR = resolved
                # Helps other PyMuPDF OCR entry points (get_textpage_ocr, etc.)
                os.environ.setdefault("TESSDATA_PREFIX", resolved)
                return resolved
        except OSError:
            continue
    _TESSDATA_DIR = None
    return None


def ocr_available() -> bool:
    return _resolve_tessdata() is not None

# =====================================================
# EXCLUSION REGISTRY — runs BEFORE pattern accept
# Indian OEM (IS/SP 46) + Tata-style title-block noise
# =====================================================

_EXCLUSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("skip_keyword", re.compile(
        r"^(sheet\s*(?:size|:|-)|sheet\s+\d+\s+of\b|rev(?:ision)?\b|date\b|scale\b|"
        r"dwg\b|drg\b|drawing\b|material\b|finish\b|note\b|"
        r"item\b|qty\b|part\b|page\b|proj\b|checked\b|approved\b|title\b|iso\b|asme\b|a[0-4]\b|"
        r"all\s+dims?\b|unless\b|otherwise\b|specified\b|detail\b|meters\b|approx\b|"
        r"millimeters\b|view\b|section\b|refer\b|general\b|welded\b|assembly\b|length\b).*",
        re.IGNORECASE)),
    # Drafter / drawing stamps: DRD180828, DCC-TMP12-v2.0
    ("drafter_stamp", re.compile(r"^DRD\d{5,}$", re.IGNORECASE)),
    ("dcc_code", re.compile(r"^DCC[-A-Z0-9.]+$", re.IGNORECASE)),
    # IDs/part codes — not plate sizes, not metric threads, not DIA callouts
    ("id_code", re.compile(
        r"^(?!\d+[x×]\d+(?:[x×]\d+)?$)"
        r"(?!M\d)"
        r"(?!DIA)"
        r"(?=.*[A-Za-z])[A-Za-z0-9._\-]{5,}$",
        re.IGNORECASE)),
    # IS 2102 general-tolerance note (title block)
    ("is_2102_note", re.compile(r".*AS\s+PER\s+IS\s*:?\s*2102.*", re.IGNORECASE)),
    ("is_note", re.compile(r".*AS\s+PER\s+IS\s*:?\s*\d+.*", re.IGNORECASE)),
    # IS material / spec citations: IS 513:2010 …
    ("is_material_spec", re.compile(r".*IS\s*\d+\s*:\s*\d{4}.*", re.IGNORECASE)),
    # ISO 2768 / IS roughness grade table words
    ("iso_2768_grade", re.compile(
        r".*\b(EXTRA\s+COARSE|COARSE|MEDIUM|FINE|VERY\s+FINE)\b.*",
        re.IGNORECASE)),
    ("roughness_key", re.compile(
        r"^X?\s*0+(\s+0+)*(\s+X\s*0+)*\s*(EXTRA\s+COARSE|COARSE|MEDIUM|FINE)?$",
        re.IGNORECASE)),
    ("thread_class_note", re.compile(r"^CLASS\s+\d+[HhGgEe]$", re.IGNORECASE)),
    # QTY / (x4) quantity markers — SP 46 practice
    ("qty_marker", re.compile(
        r"^\(?\s*[x×]\s*\d+\s*\)?$|"
        r"^QTY\b.*|"
        r"^\d+\s*NOS\.?$|"
        r"^NOS\.?$",
        re.IGNORECASE)),
    # Note-reference numbers (not dimensions): SEE NOTE 3 / NOTE-3
    ("note_ref", re.compile(
        r"^(?:SEE\s+)?NOTES?\s*:?-?\s*\d+$|"
        r"^PRESS\s*FIT\b.*NOTE.*",
        re.IGNORECASE)),
    # Length stated in meters only (mm closed/open lengths are real dims)
    ("meters_note", re.compile(
        r".*\d+(?:[.,]\d+)?\s*METERS?\b.*",
        re.IGNORECASE)),
    ("grid_zone", re.compile(r"^[A-Z]$")),  # sheet letter columns only; not R10
    # Lone 1-digit counters / item markers — 2-digit mm dims are common on fab drawings
    ("bare_int", re.compile(r"^\d$")),
    ("long_int", re.compile(r"^\d{6,}$")),  # part numbers; 4–5 digit overall dims OK
    ("scale_ratio", re.compile(r"^\d+\s*:\s*\d+$")),
    ("date_like", re.compile(r".*\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}.*", re.IGNORECASE)),
    # Title-block Ra / GD&T-looking micro values — only accept via FCF / roughness
    # geometry paths (bare 0.05/0.2 spam title blocks and limit notes).
    ("surface_ra", re.compile(r"^(?:0\.025|0\.05|0\.1|0\.2|0\.4)$")),
]

# Patterns treated as "Indian-practice specific" in audit breakdowns
_INDIAN_PATTERN_NAMES = frozenset({
    "fit_class",
    "bsp_thread_R",
    "bsp_thread_G",
    "diameter_O",
    "diameter_O_suffix",
    "metric_thread",
    "is_chamfer",
})

# Title-block identity fields (drawing / rev / part) — balloon these intentionally
_IDENTITY_LABEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("drawing_no", re.compile(
        r"^(?:DRAWING|DWG|DRG)\.?\s*NO\.?\s*:?$|"
        r"^(?:DRAWING|DWG|DRG)\.?\s*NUMBER\s*:?$",
        re.IGNORECASE)),
    ("revision", re.compile(
        r"^REV(?:ISION)?\.?\s*:?$|"
        r"^REV\.?\s*FOLIO$",
        re.IGNORECASE)),
    ("part_no", re.compile(
        r"^PART\s*(?:NO\.?|NUMBER|#)\s*:?$|"
        r"^P\s*/\s*N\s*:?$|"
        r"^P/?N$",
        re.IGNORECASE)),
]

_IDENTITY_COMBINED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("drawing_no", re.compile(
        r"^(?:DRAWING|DWG|DRG)\.?\s*NO\.?\s*:?\s*"
        r"([A-Z]{0,4}\d{5,}(?:[-./][A-Z0-9]+)*)$",
        re.IGNORECASE)),
    ("revision", re.compile(
        r"^REV(?:ISION)?\.?\s*:?\s*([A-Z0-9]{1,3})$",
        re.IGNORECASE)),
    ("part_no", re.compile(
        r"^(?:PART\s*(?:NO\.?|NUMBER|#)|P\s*/\s*N|P/?N)\s*:?\s*"
        r"([A-Z]{0,4}\d{5,}(?:[-./][A-Z0-9]+)*)$",
        re.IGNORECASE)),
]

_DRAWING_VALUE_RE = re.compile(
    r"^(?:DRD|DCC)?\d{6,}(?:[-./][A-Z0-9]+)*$|"
    r"^\d{6,}[-./][A-Z0-9]+(?:[-./][A-Z0-9]+)*$",
    re.IGNORECASE)
# Sheet rev: letter, letter+digit, or 01–99 (not bare 0)
_REV_VALUE_RE = re.compile(r"^(?:[A-Z]|[A-Z]\d|[1-9]\d?|0[1-9])$")
_PART_VALUE_RE = re.compile(
    r"^(?:DRD)?\d{6,}(?:[-./][A-Z0-9]+)*$",
    re.IGNORECASE)


def _in_identity_zone(x: float, y: float, page_rect: fitz.Rect) -> bool:
    """
    Title-block / border identity fields live in the top strip, bottom strip,
    or right-hand title column — not mid-sheet BOM tables.
    """
    w, h = page_rect.width, page_rect.height
    top = y < page_rect.y0 + h * 0.18
    bottom = y > page_rect.y0 + h * 0.78
    right = x > page_rect.x0 + w * 0.62
    return top or bottom or right


def _in_part_drawing_zone(x: float, y: float, page_rect: fitz.Rect) -> bool:
    """
    Main drawing/part numbers sit in the bottom or right title block.
    Top strips often hold revision-history P/N columns — skip those.
    """
    w, h = page_rect.width, page_rect.height
    bottom = y > page_rect.y0 + h * 0.72
    right = x > page_rect.x0 + w * 0.58
    top_right = (y < page_rect.y0 + h * 0.22) and (x > page_rect.x0 + w * 0.55)
    return bottom or right or top_right


# =====================================================
# DIMENSION PATTERN REGISTRY — (name, regex, category)
# Order matters: BSP R-fraction BEFORE radius; O+decimal for ⌀.
# =====================================================

_DIMENSION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # --- diameter (IS 10714) — symbol forms first ---
    # Optional qty (xN) / TYP / THRU HOLES suffixes (common on Tata vertical callouts)
    ("diameter_symbol", re.compile(
        r"^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?"
        r"(?:\s*[HhGgFfEeNnPp]\d{1,2}(?:\s*/\s*[HhGgFfEeNnPp]\d{1,2})?)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL|THRU|THROUGH)(?:\s*HOLES?)?)?$",
        re.IGNORECASE), "diameter"),
    # O as ⌀ substitute — ONLY when adjacent to a decimal (avoids NOS/COARSE)
    ("diameter_O", re.compile(
        r"^O\s*\d+[.,]\d+(?:\s*(?:THRU|THROUGH))?(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "diameter"),
    ("diameter_O_suffix", re.compile(
        r"^\d+[.,]\d+\s*O(?:\s*(?:THRU|THROUGH))?(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "diameter"),
    ("diameter_dia", re.compile(
        r"^(?:DIA|DIAM)\.?\s*\d+(?:[.,]\d+)?"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?(?:\s*deep)?)?"
        r"(?:\s*holes?(?:-\d+\s*nos)?)?"
        r"(?:\s*\(\s*[x×]?\s*\d+\s*\))?$|"
        r"^\d+(?:[.,]\d+)?\s*(?:DIA|DIAM)\.?$",
        re.IGNORECASE), "diameter"),
    ("spherical_dia", re.compile(
        r"^S[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "diameter"),
    # Multi-line hole callout kept as one token after merge
    ("diameter_thru_holes", re.compile(
        r"^[Ø⌀φΦ∅]?\s*\d+(?:[.,]\d+)?\s*\(\s*[x×]?\s*\d+\s*\)\s*"
        r"(?:THRU|THROUGH)\s*HOLES?$|"
        r"^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?\s*\(\s*[x×]?\s*\d+\s*\)\s*"
        r"(?:THRU|THROUGH)\s*HOLES?$",
        re.IGNORECASE), "diameter"),
    # OD / ID tube callouts (shop fab notes)
    ("od_id", re.compile(
        r"^(?:OD|ID)\s*\d+(?:[.,]\d+)?$|"
        r"^OD\s*\d+(?:[.,]\d+)?\s*[x×]\s*ID\s*\d+(?:[.,]\d+)?"
        r"(?:\s*-\s*\d+\s*long)?$",
        re.IGNORECASE), "diameter"),
    # Bush / pin OD notes on BOM: BUSH DIA-60- …
    ("bush_dia", re.compile(
        r"^BUSH\s*DIA\s*-?\s*\d+(?:[.,]\d+)?\b.*",
        re.IGNORECASE), "diameter"),
    # Ext / Int length callouts (European fab)
    ("ext_int", re.compile(
        r"^\d+(?:[.,]\d+)?\s*(?:Ext|Int|EXT|INT)\.?$",
        re.IGNORECASE), "linear"),
    # Chain dims wrapped in equals: =770±2=
    ("equals_tol", re.compile(
        r"^=\s*\d+(?:[.,]\d+)?\s*±\s*\d+(?:[.,]\d+)?\s*=$"
    ), "tolerance"),
    # Labeled geometric notes: Bore dia : 130 mm / Stroke : 1100 mm
    ("labeled_mm", re.compile(
        r"^(?:bore\s*dia|rod\s*dia|stroke|closed\s*length|open\s*length|"
        r"overall\s*length|width|height|depth|thickness|pitch|centres?)\s*:?\s*"
        r"\d+(?:[.,]\d+)?\s*mm$",
        re.IGNORECASE), "linear"),
    ("labeled_mm_generic", re.compile(
        r"^[A-Za-z][A-Za-z0-9\s/.\-]{0,28}:\s*\d+(?:[.,]\d+)?\s*mm$",
        re.IGNORECASE), "linear"),
    # Min / Max travel ranges on hydraulic sheets
    ("min_max_range", re.compile(
        r"^Min\s*\d+(?:[.,]\d+)?\s*(?:&|and)\s*Max\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "linear"),
    # Plate / section sizes — allow spaces: 1050 X 55, 776 X 25 X 25, 12x12
    ("plate_size", re.compile(
        r"^\d{2,}\s*[x×]\s*\d{2,}(?:\s*[x×]\s*\d{1,3})?"
        r"(?:\s*-\s*\d{2,5}\s*(?:long|nos))?$",
        re.IGNORECASE), "linear"),
    # Intermittent / stitch weld length pitch: 50x (20) x50
    ("weld_pitch", re.compile(
        r"^\d+(?:[.,]\d+)?\s*[x×]\s*"
        r"\(\s*\d+(?:[.,]\d+)?\s*\)\s*"
        r"[x×]\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "weld"),
    # IS 813 fillet size-length-pitch on weld leader: 6-50-100 / 6 - 50 - 100
    ("weld_size_len_pitch", re.compile(
        r"^(?:[1-9]|1[0-2])\s*-\s*\d{1,4}\s*-\s*\d{1,4}$"
    ), "weld"),
    # Dual unit mm (inch): 40.0 (1.57)
    ("dual_unit", re.compile(
        r"^\d+[.,]\d+\s*\(\s*\d+[.,]\d+\s*\)$"
    ), "linear"),
    # Sheet-metal bend callouts
    ("bend_radius", re.compile(
        r"^BEND\s*R\s*\d+(?:[.,]\d+)?(?:\s*(?:TYP\.?|TYPICAL))?$|"
        r"^MIN\s*BEND\s*R(?:ADIUS)?\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*THK\.?)?$",
        re.IGNORECASE), "feature"),
    # Knurling / spotface compound notes
    ("knurl", re.compile(
        r"^KNURL\b.*\d+(?:[.,]\d+)?.*",
        re.IGNORECASE), "feature"),
    ("spotface", re.compile(
        r"^(?:SF|SPOT\s*FACE|C'?BORE|COUNTERBORE)\b.*\d+(?:[.,]\d+)?.*",
        re.IGNORECASE), "feature"),
    # Envelope requirement (ISO 8015 / IS 8000)
    ("envelope_e", re.compile(
        r"^\d+(?:[.,]\d+)?\s*Ⓔ\s*$|"
        r"^Ⓔ\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "tolerance"),
    # BOM material thickness notes (vertical BOM columns on Tata A2/A3)
    ("material_thk", re.compile(
        r"^(?:PLATE|L\s*ANGLES?|FLAT|SHEET|ERW\s*TUBE|BLOCK|TUBE)\s*-?\s*"
        r"\d+(?:[.,]\d+)?\s*THK\.?\b.*",
        re.IGNORECASE), "feature"),
    # Clearance hole class (IS/ISO fit on hole charts)
    ("clear_fit", re.compile(
        r"^CLEAR\s+[Hh]\d{1,2}$",
        re.IGNORECASE), "fit"),
    # BOM cut-length lines: a) 5500mm-4nos / a)-1095-6nos
    ("bom_cut_length", re.compile(
        r"^[a-z]\)\s*-?\d+(?:[.,]\d+)?(?:\s*mm)?"
        r"(?:\s*-\s*\d+\s*nos)?$",
        re.IGNORECASE), "linear"),
    # Clevis / plate BOM dims with qty suffix
    ("named_plate", re.compile(
        r"^(?:\d+\.\d+\s+)?clevis(?:\s+plate)?\s+"
        r"\d+[x×]\d+[x×]\d+(?:\s*-\s*\d+\s*nos)?$",
        re.IGNORECASE), "linear"),
    # Undercut / groove shop notes
    ("undercut", re.compile(
        r"^U/?C\s*-?\s*\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep$|"
        r"^UNDERCUT\s*\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?$|"
        r"^GROOVE\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)?$",
        re.IGNORECASE), "feature"),

    # --- BSP / pipe thread (IS 554) BEFORE radius — R 1/4 vs R 10.0 ---
    ("bsp_thread_R", re.compile(
        r"^R\s*\d+\s*/\s*\d+(?:\"|″)?(?:\s*(?:BSP|BSPT|BSPP))?$",
        re.IGNORECASE), "thread"),
    ("bsp_thread_G", re.compile(
        r"^(?:G\s*)?\d+(?:/\d+)?(?:\"|″)?\s*(?:BSP|BSPT|BSPP)"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep)?$|"
        r"^\d+\s*/\s*\d+(?:\"|″)?\s*(?:BSP|BSPT|BSPP)"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep)?$|"
        r"^G\s*\d+(?:/\d+)?$",
        re.IGNORECASE), "thread"),
    ("bsp_port_labeled", re.compile(
        r"^(?:pressure|breather)\s+port\s+size\s*:\s*"
        r"\d+(?:/\d+)?(?:\"|″)?\s*(?:BSP|BSPT|BSPP)$",
        re.IGNORECASE), "thread"),

    # --- radius (IS 10714) — decimal/integer after BSP fraction check ---
    ("radius", re.compile(
        r"^R\s*\d+(?:[.,]\d+)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$",
        re.IGNORECASE), "radius"),
    ("spherical_radius", re.compile(
        r"^SR\s*\d+(?:[.,]\d+)?(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "radius"),

    # --- IS 919 / IS 2709 fit classes (ISO 286) ---
    # Omit M (metric thread) and S (SECTION marks on Tata sheets).
    ("fit_class", re.compile(
        r"^\d+(?:[.,]\d+)?\s*[ABCDEFGHJKNPRTUVYZ]\d{1,2}"
        r"(?:\s*/\s*[ABCDEFGHJKNPRTUVYZ]\d{1,2})?"
        r"(?:\s*\([+\-]?\d+(?:[.,]\d+)?(?:\s*/\s*[+\-]?\d+(?:[.,]\d+)?)?\))?$",
        re.IGNORECASE), "fit"),
    ("fit_nom_min_max", re.compile(
        r"^\d+(?:[.,]\d+)?\s*(?:NOM|MIN|MAX)$",
        re.IGNORECASE), "fit"),

    # --- threads IS 4218 metric ---
    ("metric_thread", re.compile(
        r"^M\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)?"
        r"(?:-\d+\s*nos)?"
        r"(?:\s*(?:THRU|THROUGH|TAP|TAPPING|DEEP|THRU\s+TAP(?:PING)?))?"
        r"(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "thread"),
    # M6 x 15 DEEP TAPPING (x6) — vertical stacked hole callouts
    ("metric_thread_tapping", re.compile(
        r"^M\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?"
        r"\s*DEEP\s*(?:TAP(?:PING)?)?"
        r"(?:\s*\(\s*[x×X]?\s*\d+\s*\))?$",
        re.IGNORECASE), "thread"),
    # M6 - 8.0 DEEP / M6–8 DEEP
    ("metric_thread_deep", re.compile(
        r"^M\d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)?\s*DEEP$",
        re.IGNORECASE), "thread"),
    # ØM6 ↧ 10mm / OM6`10mm (depth glyph often garbled)
    ("metric_thread_depth_mm", re.compile(
        r"^[Ø⌀φΦ∅O]?\s*M\d+(?:[.,]\d+)?"
        r"\s*[\u21A7`'\u2193vV]?\s*\d+(?:[.,]\d+)?\s*mm$",
        re.IGNORECASE), "thread"),
    ("unc_thread", re.compile(
        r"^(?:#\d+|\d+/\d+)\s*-\s*\d+\s*(?:UNC|UNF|UNEF|UN)(?:\s*-?\s*[123][AB])?$",
        re.IGNORECASE), "thread"),

    # Pitch circle diameter: Ø 85.0 P.C.D. / O 85.0 P.C.D. / 85.0 PCD
    ("pcd_diameter", re.compile(
        r"^(?:[Ø⌀φΦ∅O]|DIA\.?)?\s*\d+(?:[.,]\d+)?\s*P\.?\s*C\.?\s*D\.?$",
        re.IGNORECASE), "diameter"),

    # --- angles / chamfers (IS convention — keep as one token) ---
    ("angle_deg", re.compile(
        r"^\d+(?:[.,]\d+)?\s*°(?:\s*\d{1,2}'(?:\s*\d{1,2}\")?)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?°?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$",
        re.IGNORECASE), "angle"),
    ("is_chamfer", re.compile(
        r"^\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*°"
        r"(?:\s*(?:TYP\.?|TYPICAL))?(?:\s*\([^)]*\))?$|"
        r"^\d+(?:[.,]\d+)?\s*[x×]\s*45(?:\s*(?:TYP\.?|TYPICAL))?(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "chamfer"),

    # --- tolerances / limits ---
    ("tol_symmetric", re.compile(
        r"^\d+(?:[.,]\d+)?\s*±\s*\d+(?:[.,]\d+)?$"
    ), "tolerance"),
    ("tol_unequal", re.compile(
        r"^\d+(?:[.,]\d+)?\s*[+]\s*\d+(?:[.,]\d+)?\s*/?\s*[-]\s*\d+(?:[.,]\d+)?$"
    ), "tolerance"),
    ("limit_dim", re.compile(
        r"^\d+[.,]\d+\s*/\s*\d+[.,]\d+$"
    ), "tolerance"),

    # --- thickness / depth / holes ---
    ("thickness", re.compile(
        r"^\d+(?:[.,]\d+)?\s*THK\.?$",
        re.IGNORECASE), "feature"),
    # ISO thickness letter t (often vertical): t 10.0
    ("thickness_t", re.compile(
        r"^t\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "feature"),
    ("depth", re.compile(
        r"^\d+(?:[.,]\d+)?\s*DEEP$|"
        r"^[↧]\s*\d+(?:[.,]\d+)?$",
        re.IGNORECASE), "feature"),
    ("hole_thru", re.compile(
        r"^\d+(?:[.,]\d+)?\s*[Ø⌀φΦ∅O]?\s*(?:THRU|THROUGH)\s*(?:HOLES?)?"
        r"(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "feature"),
    # Weld / feature size with TYP: 4 TYP, 3 TYP
    ("weld_typ", re.compile(
        r"^(?:[2-9]|1[0-2])\s*(?:TYP\.?|TYPICAL)$",
        re.IGNORECASE), "weld"),

    # --- reference / auxiliary ---
    ("reference", re.compile(
        r"^\(?\d+(?:[.,]\d+)?\)?\s*REF\.?(?:\s*\([^)]*\))?$",
        re.IGNORECASE), "reference"),
    ("ref_paren", re.compile(
        r"^\(\s*\d{1,5}(?:[.,]\d+)?\s*\)$"
    ), "reference"),

    ("linear_mm", re.compile(
        r"^\d+(?:[.,]\d+)?\s*mm$",
        re.IGNORECASE), "linear"),

    # Whole-mm integers + optional qty / TYP (4-digit alone ≈ part/job codes)
    ("linear_integer", re.compile(
        r"^\d{2,3}"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$",
        re.IGNORECASE), "linear"),

    # Decimal linear + optional qty / TYP: 509.3 (x2), 20.0 TYP
    ("linear_decimal", re.compile(
        r"^\d+[.,]\d+"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$",
        re.IGNORECASE), "linear"),
]


@dataclass
class Characteristic:
    balloon: int
    page: int
    text: str
    x: float
    y: float
    width: float
    height: float
    category: str = ""
    pattern: str = ""
    family: str = ""  # "indian" | "generic"
    # Leader balloon circle centre (PDF unrotated space). When set, redraw
    # keeps the circle here instead of re-running placement.
    bx: float | None = None
    by: float | None = None
    # Arrow tip in PDF space (defaults to dim centre when unset).
    tx: float | None = None
    ty: float | None = None
    # Arrowhead scale (1.0 = default).
    arrow_size: float | None = None


def _pattern_family(pattern_name: str | None) -> str:
    if not pattern_name:
        return ""
    return "indian" if pattern_name in _INDIAN_PATTERN_NAMES else "generic"


def _identity_value_ok(kind: str, value: str) -> bool:
    v = _clean(value)
    if not v:
        return False
    if kind == "drawing_no":
        return bool(_DRAWING_VALUE_RE.match(v))
    if kind == "revision":
        # Avoid zone letters alone unless 0-9 or letter+digit; single A–F often zone
        # Title-block REV values are still accepted when tied to a REV label.
        return bool(_REV_VALUE_RE.match(v)) and v.upper() not in {"OF", "NO", "BY"}
    if kind == "part_no":
        return bool(_PART_VALUE_RE.match(v))
    return False


def _identity_display(kind: str, value: str) -> str:
    v = _clean(value)
    if kind == "drawing_no":
        return f"DRAWING NO. {v}"
    if kind == "revision":
        return f"REV. {v}"
    if kind == "part_no":
        return f"PART NO. {v}"
    return v


def _is_identity_combined(text: str) -> tuple[bool, str | None, str | None]:
    """Match 'DRAWING NO. 123…' / 'REV. K' / 'PART NO. …' on one span."""
    t = _clean(text)
    for kind, pat in _IDENTITY_COMBINED_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        value = m.group(1)
        if _identity_value_ok(kind, value):
            return True, kind, "identity"
    return False, None, None


def _identity_field_candidates(
    items: list[tuple[str, fitz.Rect, float, tuple]],
    page_rect: fitz.Rect | None = None,
) -> list[tuple[str, fitz.Rect, float, str]]:
    """
    Balloon drawing number, revision, and part number from title-block
    labels + nearby values (often split across separate text spans).
    Returns (display_text, rect, size, pattern_name).
    """
    out: list[tuple[str, fitz.Rect, float, str]] = []
    used_value_idx: set[int] = set()

    # 1) Already-combined spans
    for text, rect, size, _dir in items:
        ok, kind, _cat = _is_identity_combined(text)
        if not ok or kind is None:
            continue
        if page_rect is not None:
            if kind in {"part_no", "drawing_no"}:
                if not _in_part_drawing_zone(rect.x0, rect.y0, page_rect):
                    continue
            elif not _in_identity_zone(rect.x0, rect.y0, page_rect):
                continue
        m = None
        for k, pat in _IDENTITY_COMBINED_PATTERNS:
            if k != kind:
                continue
            m = pat.match(_clean(text))
            if m:
                break
        value = m.group(1) if m else _clean(text)
        out.append((_identity_display(kind, value), rect, size, kind))

    # 2) Label + nearby value
    for i, (text, rect, size, _dir) in enumerate(items):
        t = _clean(text)
        kind = None
        for k, pat in _IDENTITY_LABEL_PATTERNS:
            if pat.match(t):
                kind = k
                break
        if kind is None:
            continue
        # Skip REV.BY style (drafter field, not sheet revision)
        if re.match(r"^REV\.?\s*BY$", t, re.IGNORECASE):
            continue
        if page_rect is not None:
            if kind in {"part_no", "drawing_no"}:
                if not _in_part_drawing_zone(rect.x0, rect.y0, page_rect):
                    continue
            elif not _in_identity_zone(rect.x0, rect.y0, page_rect):
                continue

        best_j = None
        best_score = 1e9
        lcx = (rect.x0 + rect.x1) / 2
        lcy = (rect.y0 + rect.y1) / 2
        # Value is usually to the right or just below the label
        reach = max(size, 8.0) * 8.0
        for j, (t2, r2, s2, _d2) in enumerate(items):
            if i == j or j in used_value_idx:
                continue
            v = _clean(t2)
            if not _identity_value_ok(kind, v):
                continue
            vcx = (r2.x0 + r2.x1) / 2
            vcy = (r2.y0 + r2.y1) / 2
            dist = math.hypot(vcx - lcx, vcy - lcy)
            if dist > reach:
                continue
            # Prefer right-of / below label (title-block layout)
            right_bonus = 0.0 if vcx >= rect.x0 - 2 else 40.0
            below_bonus = 0.0 if vcy >= rect.y0 - 2 else 25.0
            # Prefer fuller tokens (01 over 0, 1315108-RAW over short noise)
            score = dist + right_bonus + below_bonus - min(len(v), 16) * 0.8
            if score < best_score:
                best_score = score
                best_j = j
        if best_j is None:
            continue
        vt, vr, vs, _ = items[best_j]
        used_value_idx.add(best_j)
        out.append(
            (
                _identity_display(kind, vt),
                fitz.Rect(rect) | fitz.Rect(vr),
                max(size, vs),
                kind,
            )
        )

    # Dedupe identical identity fields (border stamps repeat drawing nos)
    deduped: list[tuple[str, fitz.Rect, float, str]] = []
    seen: set[tuple[str, str]] = set()
    for text, rect, size, pattern in out:
        key = (_norm_dim_text(text), pattern)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((text, rect, size, pattern))
    return deduped


def _clean(text: str) -> str:
    # PDF often embeds Latin-1 ± (0xB1) and ° (0xB0)
    t = text.replace("\xb1", "±").replace("\xb0", "°")
    t = t.replace("\u00b1", "±").replace("\u00b0", "°")
    t = t.replace("\ufffd", "°")
    # Some CAD fonts map degree/plus-minus to private-use / odd bytes
    t = t.replace("\x9f", "°").replace("\x81", "±")
    t = t.replace("\n", " ").replace("\r", " ")
    t = t.replace("＋", "+").replace("−", "-").replace("–", "-")
    t = t.replace("∅", "⌀")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _exclusion_reason(text: str) -> str | None:
    """Return exclusion reason name, or None if text may be a dimension."""
    t = _clean(text)
    if not t:
        return "empty"
    if len(t) > 72:
        return "too_long"
    for name, pat in _EXCLUSION_PATTERNS:
        if pat.match(t):
            return name
    return None


def _is_dimension_verbose(text: str) -> tuple[bool, str | None, str | None]:
    """
    Pattern match only (no exclusion).
    Returns (matched, pattern_name, category).
    """
    t = _clean(text)
    if not t or not re.search(r"\d", t):
        return False, None, None
    for name, pat, category in _DIMENSION_PATTERNS:
        if pat.match(t):
            return True, name, category
    return False, None, None


def _looks_like_balloon_number(text: str) -> bool:
    # Only lone 1-digit markers — 2-digit values are often real mm dims
    return bool(re.match(r"^\d$", _clean(text)))


def _on_sheet_border(
    x: float,
    y: float,
    page_rect: fitz.Rect,
    *,
    margin_frac: float = 0.045,
) -> bool:
    """True when a glyph sits in the outer sheet-border / frame band."""
    short = min(page_rect.width, page_rect.height)
    margin = max(20.0, min(90.0, short * margin_frac))
    return (
        x < page_rect.x0 + margin
        or x > page_rect.x1 - margin
        or y < page_rect.y0 + margin
        or y > page_rect.y1 - margin
    )


def _is_border_grid_mark(text: str, x: float, y: float, page_rect: fitz.Rect) -> bool:
    """
    Sheet border coordinate marks: A B C … along an edge, or 1 2 3 … / 01.
    Never balloon these — they are frame references, not dimensions.
    """
    if not _on_sheet_border(x, y, page_rect):
        return False
    t = _clean(text)
    return bool(
        re.match(r"^[A-Z]$", t)
        or re.match(r"^\d{1,2}$", t)
        or re.match(r"^[A-Z]\d{1,2}$", t)  # rare zone tokens
    )


def _looks_like_grid_number(text: str, x: float, y: float, page_rect: fitz.Rect) -> bool:
    """Audit helper: reject border-grid marks (position-aware)."""
    return _is_border_grid_mark(text, x, y, page_rect)


def _looks_like_id_code(text: str) -> bool:
    t = _clean(text)
    return bool(
        re.match(
            r"^(?!\d+[x×]\d+(?:[x×]\d+)?$)"
            r"(?!M\d)"
            r"(?!DIA)"
            r"(?=.*[A-Za-z])[A-Za-z0-9._\-]{5,}$",
            t,
            re.IGNORECASE)
        or re.match(r"^DRD\d{5,}$", t, re.IGNORECASE)
    )


def _in_title_block(
    x: float,
    y: float,
    page_rect: fitz.Rect,
    *,
    bottom_frac: float = 0.90,
    right_frac: float = 0.90,
) -> bool:
    """
    Title-block / border band — wider than before to catch roughness keys,
    grade tables, and drafter stamps that live along the sheet edge.
    Uses unrotated mediabox coords when callers pass mediabox.
    Higher frac = smaller excluded band (used by recovery passes).
    """
    w, h = page_rect.width, page_rect.height
    # Bottom strip or right strip (typical title block / revision columns).
    # Keep the band narrow so note blocks just above the border still balloon.
    return (
        (y > page_rect.y0 + h * bottom_frac)
        or (x > page_rect.x0 + w * right_frac and y > page_rect.y0 + h * 0.60)
    )


def _in_title_table_band(
    x: float,
    y: float,
    size: float,
    page_rect: fitz.Rect,
) -> bool:
    """
    Finish / tolerance / sheet-meta tables along the right edge of mediabox
    (often the bottom of a Rotate-90 landscape sheet). Small font only —
    real fab dims on this corpus are ~8–12 pt.
    """
    w, h = page_rect.width, page_rect.height
    if size >= 8.0:
        return False
    # Right ~14% column (finish microns, CLEAR H12, sheet OF n, scale…)
    if x > page_rect.x0 + w * 0.86:
        return True
    # Lower-right title corner (broader)
    if x > page_rect.x0 + w * 0.72 and y > page_rect.y0 + h * 0.72:
        return True
    return False


# =====================================================
# ZONE + TILE DETECTION — drawing / BOM / notes / title
# =====================================================

_BOM_LABELS = (
    "BILL OF MATERIAL",
    "BILL OF MATERIALS",
    "B.O.M.",
    "B.O.M",
)
_NOTES_LABELS = (
    "WELDING NOTES",
    "GENERAL NOTES",
    "FINISH NOTES",
    "STRUCTURE WELDING",
    "WELD TESTING NOTES",
    "WELD TESTING",
)
_TITLE_LABELS = (
    "DRAWING NO.",
    "DRAWING NO",
    "TITLE:",
    "TOLERANCE UNLESS",
    "SHEET SIZE",
    "SCALE:",
)

# Patterns allowed inside BOM tables (material callouts — not item numbers)
_BOM_OK_PATTERNS = frozenset({
    "plate_size",
    "material_thk",
    "bush_dia",
    "od_id",
    "labeled_mm",
    "thickness_t",
    "weld_typ",
    "metric_thread",
    "diameter_dia",
    "diameter_O",
    "diameter_symbol",
    "linear_decimal",
    "linear_integer",
    "linear_mm",
    "tol_symmetric",
})

# Patterns allowed in notes columns (skip narrative numbers)
_NOTES_OK_PATTERNS = frozenset({
    "weld_size",
    "weld_typ",
    "angle_deg",
    "diameter_symbol",
    "diameter_dia",
    "diameter_O",
    "metric_thread",
    "metric_thread_tapping",
    "metric_thread_deep",
    "metric_thread_depth_mm",
    "pcd_diameter",
    "fit_class",
    "clear_fit",
    "radius",
    "plate_size",
    "material_thk",
    "bush_dia",
    "od_id",
    "labeled_mm",
    "thickness_t",
    "linear_decimal",
    "linear_integer",
    "linear_mm",
    "tol_symmetric",
    "tol_unequal",
    "reference",
    "ref_paren",
    "surface_finish",
    "gdt_tolerance",
})

# Real measurable categories — may sit in mis-expanded title/notes bands on dense sheets
_DIM_CATEGORIES = frozenset({
    "linear",
    "tolerance",
    "angle",
    "radius",
    "diameter",
    "thread",
    "fit",
    "chamfer",
    "feature",
    "weld",
    "reference",
    "surface",
    "gdt",
})

# Strict auto-balloon: dimensions only (no title block, notes, refs, welds, GD&T symbols)
_STRICT_BALLOON_CATEGORIES = frozenset({
    "linear",
    "tolerance",
    "angle",
    "radius",
    "diameter",
    "thread",
    "fit",
    "chamfer",
    "feature",
})


def _strict_dimension_only() -> bool:
    import os
    return os.environ.get("SMORX_STRICT_DIM", "1").strip().lower() not in {"0", "false", "no", "off"}


def _category_allowed_for_balloon(category: str, pattern: str = "") -> bool:
    if not _strict_dimension_only():
        return category in _DIM_CATEGORIES or category in {"", "weld", "reference", "surface", "gdt"}
    if category in _STRICT_BALLOON_CATEGORIES:
        return True
    return False


@dataclass
class PageZones:
    """Keyword-expanded regions on one page (unrotated mediabox space)."""
    bom: list[fitz.Rect]
    notes: list[fitz.Rect]
    title: list[fitz.Rect]
    bound: fitz.Rect


def _search_labels(page: fitz.Page, labels: tuple[str, ...]) -> list[fitz.Rect]:
    hits: list[fitz.Rect] = []
    for lab in labels:
        try:
            found = page.search_for(lab, quads=False)
        except Exception:
            found = []
        for r in found or []:
            hits.append(fitz.Rect(r))
    return hits


def _expand_zone_rect(
    label: fitz.Rect,
    bound: fitz.Rect,
    kind: str,
) -> fitz.Rect:
    """Grow a keyword hit into the table / block it heads."""
    w, h = bound.width, bound.height
    vertical = label.height > label.width * 1.3
    if kind == "bom":
        if vertical:
            return fitz.Rect(
                max(bound.x0, label.x0 - 40),
                max(bound.y0, label.y0 - 160),
                min(bound.x1, label.x0 + w * 0.48),
                min(bound.y1, label.y1 + h * 0.55),
            )
        return fitz.Rect(
            max(bound.x0, label.x0 - 50),
            max(bound.y0, label.y0 - 80),
            min(bound.x1, label.x1 + w * 0.55),
            min(bound.y1, label.y0 + h * 0.55),
        )
    if kind == "notes":
        # Keep notes bands narrow so they don't swallow adjacent BOM columns
        if vertical:
            return fitz.Rect(
                max(bound.x0, label.x0 - 8),
                max(bound.y0, label.y0 - 15),
                min(bound.x1, label.x0 + w * 0.12),
                min(bound.y1, label.y1 + h * 0.18),
            )
        return fitz.Rect(
            max(bound.x0, label.x0 - 15),
            max(bound.y0, label.y0 - 8),
            min(bound.x1, label.x1 + w * 0.14),
            min(bound.y1, label.y0 + h * 0.18),
        )
    # title / identity block around label — keep tight
    return fitz.Rect(
        max(bound.x0, label.x0 - 30),
        max(bound.y0, label.y0 - 30),
        min(bound.x1, label.x1 + w * 0.14),
        min(bound.y1, label.y1 + h * 0.12),
    )


def _build_page_zones(page: fitz.Page) -> PageZones:
    """
    Infer BOM / notes / title regions from labels + classic title-band geometry.
    Anything outside these rects is treated as the drawing (geometry) zone.
    """
    bound = _page_bound(page)
    w, h = bound.width, bound.height

    bom = [_expand_zone_rect(r, bound, "bom") for r in _search_labels(page, _BOM_LABELS)]
    notes = [_expand_zone_rect(r, bound, "notes") for r in _search_labels(page, _NOTES_LABELS)]
    title = [_expand_zone_rect(r, bound, "title") for r in _search_labels(page, _TITLE_LABELS)]

    # Classic title-block bands — keep narrow so section views / overall
    # dims on the left & bottom are not mislabeled as title.
    title.append(fitz.Rect(
        bound.x0 + w * 0.84, bound.y0 + h * 0.70, bound.x1, bound.y1,
    ))
    title.append(fitz.Rect(
        bound.x0 + w * 0.65, bound.y0 + h * 0.93, bound.x1, bound.y1,
    ))
    # Top-right revision strip (narrow)
    title.append(fitz.Rect(
        bound.x0 + w * 0.78, bound.y0, bound.x1, bound.y0 + h * 0.10,
    ))
    return PageZones(bom=bom, notes=notes, title=title, bound=bound)


def _point_in_rects(x: float, y: float, rects: list[fitz.Rect]) -> bool:
    for r in rects:
        if r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1:
            return True
    return False


def _zone_at(x: float, y: float, zones: PageZones) -> str:
    """
    Classify a point. Priority: BOM > notes > title > drawing.
    BOM/notes win over title so material tables aren't treated as title-only.
    """
    if _point_in_rects(x, y, zones.bom):
        return "bom"
    if _point_in_rects(x, y, zones.notes):
        return "notes"
    if _point_in_rects(x, y, zones.title):
        return "title"
    return "drawing"


def _zone_allows_pattern(zone: str, pattern: str, category: str) -> bool:
    """Policy: what may be ballooned in each zone."""
    if _strict_dimension_only():
        # Strict: main drawing geometry only — never BOM, notes, or title block.
        return zone == "drawing"
    if zone == "drawing":
        return True
    if zone == "title":
        # Title block is OCR-only (inspection report headers) — never balloon.
        return False
    if zone == "bom":
        return pattern in _BOM_OK_PATTERNS or category in _DIM_CATEGORIES
    if zone == "notes":
        return pattern in _NOTES_OK_PATTERNS or category in _DIM_CATEGORIES
    return False


def _drawing_tiles(
    zones: PageZones,
    *,
    rows: int = 2,
    cols: int = 3,
) -> list[fitz.Rect]:
    """
    Tile the page and keep tiles that are mostly outside BOM/notes/title.
    Used for recovery passes with looser merge inside the geometry area.
    """
    bound = zones.bound
    tiles: list[fitz.Rect] = []
    tw = bound.width / cols
    th = bound.height / rows
    pad = 36.0  # ~0.5 in overlap each side (~72 pt shared band between tiles)
    for ri in range(rows):
        for ci in range(cols):
            tile = fitz.Rect(
                bound.x0 + ci * tw - pad,
                bound.y0 + ri * th - pad,
                bound.x0 + (ci + 1) * tw + pad,
                bound.y0 + (ri + 1) * th + pad,
            ) & bound
            # Sample tile center — skip if center sits in non-drawing zone
            cx = (tile.x0 + tile.x1) / 2
            cy = (tile.y0 + tile.y1) / 2
            if _zone_at(cx, cy, zones) != "drawing":
                continue
            tiles.append(tile)
    return tiles


def _early_reject_reason(text: str) -> str | None:
    reason = _exclusion_reason(text)
    if reason:
        return f"rejected: {reason}"
    return None


def _is_dimension(text: str) -> bool:
    """Production classifier: exclusion registry first, then pattern registry."""
    t = _clean(text)
    if _exclusion_reason(t):
        return False
    matched, name, category = _is_dimension_verbose(t)
    if not matched:
        return False
    # Sheet-size false fits: "11 A0", "A1" style paper refs near title block text
    if category == "fit" and re.match(r"^\d+(?:[.,]\d+)?\s*A[0-4]$", t, re.IGNORECASE):
        return False
    return True


def _page_bound(page: fitz.Page) -> fitz.Rect:
    """
    Drawing / Shape coordinates are always in the *unrotated* mediabox space,
    even when page.rotation != 0. page.rect is only for the rotated view.
    """
    return fitz.Rect(page.mediabox)


def _to_draw_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """Keep text rects in draw space (unrotated). No rotation_matrix here."""
    _ = page
    return fitz.Rect(rect)


def _iter_text_lines(page: fitz.Page):
    """
    Yield (text, page_space_rect, font_size, dir) from get_text('dict').
    Keeps rotated-line metadata (dir) that words-mode discards.
    """
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_no = block.get("number", 0)
        for line_i, line in enumerate(block.get("lines", [])):
            spans = line.get("spans") or []
            if not spans:
                continue
            raw = "".join(s.get("text", "") for s in spans)
            text = _clean(raw)
            if not text:
                continue
            bbox = line.get("bbox") or spans[0]["bbox"]
            rect = _to_draw_rect(page, fitz.Rect(bbox))
            size = float(spans[0].get("size") or max(rect.height, rect.width) * 0.8)
            direction = tuple(line.get("dir") or (1.0, 0.0))
            yield text, rect, size, direction, block_no, line_i, raw


def _merge_stacked_glyphs(
    items: list[tuple[str, fitz.Rect, float, tuple]],
    merge_scale: float = 1.0,
) -> list[tuple[str, fitz.Rect, float, tuple]]:
    """
    Merge single-glyph digits stacked along the text direction into one number.
    Common on European CAD exports where vertical dims arrive as 1+2+5+0.
    merge_scale > 1 widens gaps for cross-check recovery passes.
    """
    if len(items) < 2:
        return items
    ms = max(1.0, float(merge_scale))

    def _is_glyph(t: str) -> bool:
        t = _clean(t)
        return bool(re.match(r"^[\d.,]$", t)) or t in {"±", "+", "-"}

    used = [False] * len(items)
    out: list[tuple[str, fitz.Rect, float, tuple]] = []

    for i, (text, rect, size, direction) in enumerate(items):
        if used[i] or not _is_glyph(text):
            continue
        dx, dy = float(direction[0]), float(direction[1])
        norm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / norm, dy / norm
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        cluster = [(i, text, rect, size, direction)]
        used[i] = True

        changed = True
        while changed:
            changed = False
            for j, (t2, r2, s2, d2) in enumerate(items):
                if used[j] or not _is_glyph(t2):
                    continue
                dx2, dy2 = float(d2[0]), float(d2[1])
                # same or opposite reading direction
                if abs(dx2 * ux + dy2 * uy) < 0.7:
                    continue
                cx2 = (r2.x0 + r2.x1) / 2
                cy2 = (r2.y0 + r2.y1) / 2
                px, py = -uy, ux
                perp = abs((cx2 - cx) * px + (cy2 - cy) * py)
                if perp > max(size, 6.0) * 0.6 * ms:
                    continue
                ok_gap = False
                for _, _, r3, s3, _ in cluster:
                    c3x = (r3.x0 + r3.x1) / 2
                    c3y = (r3.y0 + r3.y1) / 2
                    if math.hypot(cx2 - c3x, cy2 - c3y) <= max(size, s3, 6.0) * 1.85 * ms:
                        ok_gap = True
                        break
                if not ok_gap:
                    continue
                used[j] = True
                cluster.append((j, t2, r2, s2, d2))
                changed = True

        if len(cluster) == 1:
            used[i] = False
            continue

        cluster.sort(
            key=lambda it: (
                ((it[2].x0 + it[2].x1) / 2 - cx) * ux
                + ((it[2].y0 + it[2].y1) / 2 - cy) * uy
            )
        )
        merged = "".join(_clean(t) for _, t, _, _, _ in cluster)
        # reject border grid runs like 123456 spaced across the sheet (already gap-limited)
        if not re.search(r"\d", merged):
            for idx, *_ in cluster:
                used[idx] = False
            continue
        mrect = fitz.Rect(cluster[0][2])
        for _, _, r, _, _ in cluster[1:]:
            mrect |= fitz.Rect(r)
        out.append((merged, mrect, max(s for _, _, _, s, _ in cluster), cluster[0][4]))

    for i, item in enumerate(items):
        if not used[i]:
            out.append(item)
    return out


def _merge_nearby_tol(
    items: list[tuple[str, fitz.Rect, float, tuple]],
    merge_scale: float = 1.0,
) -> list[tuple[str, fitz.Rect, float, tuple]]:
    """
    Distance-based merge for split callouts:
      40 + ±0.15  → 40±0.15
      8  + THK.     → 8 THK.
      355 REF. + (x2) → 355 REF. (x2)
    merge_scale > 1 widens host search for cross-check recovery.
    """
    if not items:
        return items
    ms = max(1.0, float(merge_scale))

    used = [False] * len(items)
    out: list[tuple[str, fitz.Rect, float, tuple]] = []

    def _is_tol(t: str) -> bool:
        return bool(re.match(r"^±\s*\d+(?:[.,]\d+)?$", _clean(t)))

    def _is_unit_suffix(t: str) -> bool:
        # Fit suffixes omit S (section marks S22/S40) and M (threads)
        return bool(re.match(
            r"^(THK\.?|THRU|THROUGH|TAP|TAPPING|DEEP|NOM|MIN|MAX|REF\.?|"
            r"TYP\.?|TYPICAL|(?:THRU|THROUGH)\s*HOLES?|"
            r"P\.?\s*C\.?\s*D\.?|EQUISPACED|"
            r"[ABCDEFGHJKNPRTUVYZ]\d{1,2}|\(\s*[x×X]\s*\d+\s*\))$",
            _clean(t),
            re.IGNORECASE))

    def _is_limit_frag(t: str) -> bool:
        # Stacked fit limits drawn as separate lines: +0.030 / 0
        return bool(re.match(r"^[+\-±]?\s*\d+(?:[.,]\d+)?$", _clean(t))) and (
            t.strip().startswith(("+", "-", "±")) or _clean(t) in {"0", "0.0", "0.00"}
        )

    def _is_numeric_host(t: str) -> bool:
        t = _clean(t)
        return bool(
            re.match(r"^\(?\d+(?:[.,]\d+)?\)?$", t)
            or re.match(r"^M\d+(?:[.,]\d+)?$", t, re.IGNORECASE)
            or re.match(r"^R\s*\d+(?:[.,]\d+)?$", t, re.IGNORECASE)
            or re.match(r"^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?$", t, re.IGNORECASE)
            or re.match(r"^\d+(?:[.,]\d+)?\s*REF\.?$", t, re.IGNORECASE)
            or re.match(r"^\d+(?:[.,]\d+)?\s*[ABCDEFGHJKNPRTUVYZ]\d+$", t, re.IGNORECASE)
            or re.match(r"^\d+(?:[.,]\d+)?\s*°$", t)
        )

    def _nearest_host(i: int, rect: fitz.Rect, size: float, pred) -> int | None:
        best_j = None
        best_d = 1e9
        # Vertical callouts often place (x2) beside the value — need a wider net
        thresh = max(size, 8.0) * 3.5 * ms
        for j, (t2, r2, s2, d2) in enumerate(items):
            if i == j or used[j]:
                continue
            if not pred(_clean(t2)):
                continue
            cx = (rect.x0 + rect.x1) / 2 - (r2.x0 + r2.x1) / 2
            cy = (rect.y0 + rect.y1) / 2 - (r2.y0 + r2.y1) / 2
            dist = math.hypot(cx, cy)
            if dist < best_d and dist <= thresh:
                best_d = dist
                best_j = j
        return best_j

    # Pass 1: ±tol → host
    for i, (text, rect, size, direction) in enumerate(items):
        t = _clean(text)
        if not _is_tol(t):
            continue
        best_j = _nearest_host(i, rect, size, _is_numeric_host)
        if best_j is None:
            continue
        ht, hr, hs, hd = items[best_j]
        used[i] = True
        used[best_j] = True
        out.append((_clean(ht) + t, fitz.Rect(hr) | fitz.Rect(rect), max(hs, size), hd))

    # Pass 2: unit suffix → numeric host (8 + THK. / REF + (x2))
    for i, (text, rect, size, direction) in enumerate(items):
        if used[i]:
            continue
        t = _clean(text)
        if not _is_unit_suffix(t):
            continue
        best_j = _nearest_host(i, rect, size, _is_numeric_host)
        if best_j is None:
            continue
        ht, hr, hs, hd = items[best_j]
        used[i] = True
        used[best_j] = True
        out.append((_clean(ht) + " " + t, fitz.Rect(hr) | fitz.Rect(rect), max(hs, size), hd))

    # Pass 2b: fit class + nearby limit fragments → "60 H7 (+0.030)"
    for i, (text, rect, size, direction) in enumerate(items):
        if used[i]:
            continue
        t = _clean(text)
        if not _is_limit_frag(t) or not t.lstrip().startswith("+"):
            continue
        best_j = _nearest_host(
            i, rect, size,
            lambda host: bool(re.match(r"^\d+(?:[.,]\d+)?\s*[A-Z]\d+$", host, re.IGNORECASE)),
        )
        if best_j is None:
            continue
        ht, hr, hs, hd = items[best_j]
        used[i] = True
        used[best_j] = True
        out.append((_clean(ht) + f" ({t})", fitz.Rect(hr) | fitz.Rect(rect), max(hs, size), hd))

    # Pass 3: leftovers
    for i, (text, rect, size, direction) in enumerate(items):
        if used[i]:
            continue
        out.append((_clean(text), rect, size, direction))

    return [(t, r, s, d) for t, r, s, d in out if t]


def _collect_path_points(
    drawings: list,
    cx: float,
    cy: float,
    reach: float,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        if abs((r.x0 + r.x1) / 2 - cx) > reach or abs((r.y0 + r.y1) / 2 - cy) > reach:
            continue
        for it in d.get("items") or []:
            if it[0] == "l":
                pts.append((float(it[1].x), float(it[1].y)))
                pts.append((float(it[2].x), float(it[2].y)))
            elif it[0] == "c":
                pts.append((float(it[4].x), float(it[4].y)))
    return pts


def _cluster_points(
    pts: list[tuple[float, float]],
    tol: float = 2.0,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in pts:
        matched = False
        for i, (ox, oy) in enumerate(out):
            if math.hypot(x - ox, y - oy) <= tol:
                out[i] = ((ox + x) / 2, (oy + y) / 2)
                matched = True
                break
        if not matched:
            out.append((x, y))
    return out


def _triangle_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) != 3:
        return 0.0
    (x1, y1), (x2, y2), (x3, y3) = pts
    return abs((x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0)


def _point_in_triangle(px: float, py: float, pts: list[tuple[float, float]]) -> bool:
    if len(pts) != 3:
        return False
    (x1, y1), (x2, y2), (x3, y3) = pts

    def _sign(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
        return (ax - cx) * (by - cy) - (bx - cx) * (ay - cy)

    d1 = _sign(px, py, x1, y1, x2, y2)
    d2 = _sign(px, py, x2, y2, x3, y3)
    d3 = _sign(px, py, x3, y3, x1, y1)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _best_three_corners(corners: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pick the 3 corners with the largest enclosed area."""
    if len(corners) <= 3:
        return list(corners)
    best: list[tuple[float, float]] = corners[:3]
    best_a = 0.0
    for i in range(len(corners)):
        for j in range(i + 1, len(corners)):
            for k in range(j + 1, len(corners)):
                tri = [corners[i], corners[j], corners[k]]
                a = _triangle_area(tri)
                if a > best_a:
                    best_a = a
                    best = tri
    return best


def _existing_revision_triangles(
    drawings: list,
) -> list[list[tuple[float, float]]]:
    """
    Revision / change delta marks: a number drawn inside a triangle.
    These must never be auto-ballooned.
    Returns list of 3-point triangles (and axis-aligned bbox fallbacks as
    4-corner quads stored as 3 pts from best triangle of the rect corners
    when geometry is messy).
    """
    tris: list[list[tuple[float, float]]] = []

    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        # Typical revision delta size (includes mid-size deltas; tiny fillet
        # ticks are skipped via fill+size check below)
        if not (12.0 <= max(w, h) <= 70.0 and 10.0 <= min(w, h) <= 60.0):
            continue
        ar = max(w, h) / max(min(w, h), 0.1)
        if ar > 2.6:
            continue
        items = d.get("items") or []
        lines = [it for it in items if it[0] == "l"]
        if len(lines) < 3:
            continue
        # Skip filled micro weld fillets (already handled as weld anchors)
        if max(w, h) <= 16.0 and d.get("fill"):
            continue

        # Keep meaningful edges (ignore micro zig-zag dither)
        edge_min = min(w, h) * 0.22
        raw_pts: list[tuple[float, float]] = []
        long_edges = 0
        for it in lines:
            p1, p2 = it[1], it[2]
            length = math.hypot(float(p2.x - p1.x), float(p2.y - p1.y))
            if length < edge_min:
                continue
            long_edges += 1
            raw_pts.append((float(p1.x), float(p1.y)))
            raw_pts.append((float(p2.x), float(p2.y)))

        tri: list[tuple[float, float]] | None = None
        if long_edges >= 3 and len(raw_pts) >= 6:
            corners = _cluster_points(raw_pts, tol=max(2.0, min(w, h) * 0.08))
            if 3 <= len(corners) <= 6:
                cand = _best_three_corners(corners)
                if _triangle_area(cand) >= (min(w, h) ** 2) * 0.12:
                    tri = cand

        # Fallback: treat the drawing bbox as a delta envelope
        # (CAD often strokes triangles as many short segments).
        if tri is None and long_edges >= 3 and 1.05 <= ar <= 2.4:
            tri = [
                (float(r.x0), float(r.y0)),
                (float(r.x1), float(r.y0)),
                (float((r.x0 + r.x1) / 2), float(r.y1)),
            ]

        if tri is not None:
            tris.append(tri)

    return tris


def _is_inside_revision_triangle(
    cx: float,
    cy: float,
    size: float,
    drawings: list,
    triangles: list[list[tuple[float, float]]] | None = None,
) -> bool:
    """True when a short index number sits inside a revision delta triangle."""
    _ = (size, drawings)
    if triangles is None:
        triangles = _existing_revision_triangles(drawings)
    for pts in triangles:
        if _point_in_triangle(cx, cy, pts):
            return True
        # Near the triangle centroid still counts (rotated / padded glyphs)
        tcx = sum(p[0] for p in pts) / 3.0
        tcy = sum(p[1] for p in pts) / 3.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if (
            min(xs) - 3 <= cx <= max(xs) + 3
            and min(ys) - 3 <= cy <= max(ys) + 3
            and math.hypot(cx - tcx, cy - tcy) <= max(size * 0.55, 3.0)
        ):
            return True
    return False


def _is_revision_index_text(text: str) -> bool:
    """Revision delta contents are small integers (1–999), not mm dims."""
    return bool(re.match(r"^\d{1,3}$", _clean(text)))


def _existing_item_balloon_circles(drawings: list) -> list[tuple[float, float, float]]:
    """
    Circles already on the sheet that look like *item / BOM balloons*
    (hollow callout rings), not large Ø holes.
    """
    circles: list[tuple[float, float, float]] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        # Item balloons on A0/A1 sheets are typically ~8–40 pt across
        if not (6.0 <= w <= 42.0 and 6.0 <= h <= 42.0):
            continue
        if max(w, h) / max(min(w, h), 0.1) > 1.40:
            continue
        # Solid filled discs without stroke ≈ holes / bosses — skip
        if d.get("fill") and not d.get("stroke"):
            continue
        # Skip blue/cyan markup (reviewer ink) — not item balloons
        col = d.get("color")
        if isinstance(col, (list, tuple)) and len(col) >= 3:
            r, g, b = float(col[0]), float(col[1]), float(col[2])
            if b > 0.45 and b > r + 0.15 and b > g + 0.05:
                continue
        items = d.get("items") or []
        ops = [it[0] for it in items]
        # Curve circles or polygonal rings (segment-built)
        if "c" not in ops and len(items) < 4:
            continue
        cx = (r.x0 + r.x1) / 2
        cy = (r.y0 + r.y1) / 2
        rad = max(w, h) / 2
        circles.append((cx, cy, rad))

    merged: list[tuple[float, float, float]] = []
    for cx, cy, rad in sorted(circles, key=lambda t: -t[2]):
        if any(math.hypot(cx - mx, cy - my) < max(rad, mr) * 0.55 for mx, my, mr in merged):
            continue
        merged.append((cx, cy, rad))
    return merged


def _is_inside_existing_balloon(
    cx: float,
    cy: float,
    size: float,
    drawings: list,
    circles: list[tuple[float, float, float]] | None = None,
) -> bool:
    """True when a point sits inside an existing callout / item-balloon ring."""
    if circles is None:
        circles = _existing_item_balloon_circles(drawings)
    for ocx, ocy, orad in circles:
        if math.hypot(cx - ocx, cy - ocy) <= orad * 0.92:
            return True
    return False


# Plain number already printed inside a circle (item balloon or circled dim)
_CIRCLED_PLAIN_NUMBER = re.compile(
    r"^\d{1,4}$|"  # integers: 2, 31, 49, 307
    r"^\d{1,4}[.,]\d{1,3}$"  # decimals in circle: 10.5, 12.0
)


def _point_seg_dist(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Distance from point to line segment."""
    dx, dy = x2 - x1, y2 - y1
    len2 = dx * dx + dy * dy
    if len2 <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _leader_clear(
    bx: float,
    by: float,
    br: float,
    tx: float,
    ty: float,
    obstacles: list[tuple[float, float, float]],
    *,
    clearance: float = 1.25,
) -> bool:
    """
    True when the leader from balloon rim → text tip stays clear of other
    balloon circles (no overlap / no crossing through them).
    """
    dx, dy = tx - bx, ty - by
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    # Leader starts at rim; tip stops short of text centre
    sx = bx + ux * (br + 0.4)
    sy = by + uy * (br + 0.4)
    tip_gap = 2.0
    ex = tx - ux * tip_gap
    ey = ty - uy * tip_gap
    for ocx, ocy, orad in obstacles:
        # Skip the obstacle if it is the text target itself (already skipped upstream)
        if math.hypot(ocx - tx, ocy - ty) < orad * 0.5:
            continue
        need = (orad + 1.5) * clearance
        if _point_seg_dist(ocx, ocy, sx, sy, ex, ey) < need:
            return False
    return True


def _stroke_points_near(
    drawings: list,
    cx: float,
    cy: float,
    reach: float,
) -> list[tuple[float, float]]:
    """Path endpoints near a glyph — used to detect callout rings."""
    return _collect_path_points(drawings, cx, cy, reach)


def _ring_around_glyph(
    cx: float,
    cy: float,
    size: float,
    drawings: list,
    *,
    min_sectors: int = 8,
) -> float | None:
    """
    If path strokes form a near-complete circle around (cx,cy), return radius.
    Used to detect existing item balloons drawn as many short segments.
    """
    reach = max(float(size) * 4.2, 24.0)
    pts = _stroke_points_near(drawings, cx, cy, reach)
    if len(pts) < 8:
        return None
    best_r: float | None = None
    best_cov = 0
    for factor in (0.9, 1.0, 1.15, 1.35, 1.55, 1.8, 2.1, 2.4, 2.8):
        rad = float(size) * factor
        if not (4.0 <= rad <= 30.0):
            continue
        angs: list[float] = []
        tol = max(1.0, rad * 0.26)
        for px, py in pts:
            dist = math.hypot(px - cx, py - cy)
            if abs(dist - rad) <= tol:
                angs.append(math.atan2(py - cy, px - cx))
        if len(angs) < 6:
            continue
        bins = [0] * 12
        for a in angs:
            bins[int((a + math.pi) / (2 * math.pi) * 12) % 12] += 1
        covered = sum(1 for b in bins if b > 0)
        if covered > best_cov:
            best_cov = covered
            best_r = rad
    if best_cov >= min_sectors and best_r is not None:
        return best_r
    return None


def _infer_balloon_circles_from_text(
    page: fitz.Page,
    drawings: list,
) -> list[tuple[float, float, float]]:
    """
    HARD: find plain integers already inside a callout circle.

    CAD often strokes balloons as many tiny line segments (no single 'c'
    drawing). Detect those via a ring-of-strokes test around each glyph.

    Decimals (211.0, 38.0…) are NOT inferred here — dimension arrows/arcs
    around linear dims commonly fake a partial ring and must stay balloonable.
    """
    inferred: list[tuple[float, float, float]] = []
    for text, rect, size, *_rest in _iter_text_lines(page):
        t = _clean(text)
        # Integers only — decimals use stricter live ring test in _is_circled_number
        if not re.match(r"^\d{1,4}$", t):
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        s = max(float(size), float(min(rect.width, rect.height)), 4.0)
        if s > 18.0:
            continue
        rad = _ring_around_glyph(cx, cy, s, drawings, min_sectors=9)
        if rad is None:
            continue
        inferred.append((cx, cy, rad * 1.15))

    out: list[tuple[float, float, float]] = []
    for cx, cy, rad in inferred:
        if any(math.hypot(cx - ox, cy - oy) < max(rad, orad) * 0.55 for ox, oy, orad in out):
            continue
        out.append((cx, cy, rad))
    return out


def _is_circled_number(
    text: str,
    cx: float,
    cy: float,
    size: float,
    drawings: list,
    circles: list[tuple[float, float, float]],
) -> bool:
    """
    HARD RULE — never auto-balloon a value already drawn inside a circle.

    • integers in item/BOM balloons (2, 31, 43, 49…)
    • plain decimals already inside a *complete* circle (rare)

    Tiny rings (weld-all-around ○ on weld leaders, ~2–7 pt) are ignored —
    weld sizes sit *beside* those, not inside them.

    Linear decimals next to dimension arrows must NOT be skipped — those
    arcs are not item balloons (require near-full ring coverage).
    """
    t = _clean(text)
    if not _CIRCLED_PLAIN_NUMBER.match(t):
        return False
    if size > 18.0:
        return False
    is_decimal = bool(re.search(r"[.,]\d", t))
    # Only treat as item-balloon rings (not weld-all-around micro circles)
    item_circles = [(x, y, r) for x, y, r in circles if r >= 7.0]
    if _is_inside_existing_balloon(cx, cy, size, drawings, item_circles):
        # Decimals: require the enclosing circle centre to be near the glyph
        # (avoid giant false circles swallowing nearby dims)
        if is_decimal:
            if any(math.hypot(cx - x, cy - y) <= r * 0.75 for x, y, r in item_circles):
                return True
        else:
            return True
    for ocx, ocy, orad in item_circles:
        if math.hypot(cx - ocx, cy - ocy) <= orad * (0.75 if is_decimal else 0.95):
            return True
    # Fallback ring test — decimals need a near-complete ring (not dim arrows)
    sectors = 11 if is_decimal else 9
    ring = _ring_around_glyph(cx, cy, max(size, 4.0), drawings, min_sectors=sectors)
    if ring is not None and ring >= (8.0 if is_decimal else 6.5):
        return True
    return False


def _fillet_weld_anchors(page: fitz.Page) -> list[fitz.Point]:
    """
    Approximate fillet-weld symbol locations from vector drawings.
    Fillet marks are small right-triangles on the weld reference line
    (ISO 2553 / AWS A2.4). Size of the weld (1, 3, 4, 6…) sits beside them.
    """
    anchors: list[fitz.Point] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return anchors

    for d in drawings:
        items = d.get("items") or []
        lines = [it for it in items if it[0] == "l"]
        # Fillet △ is typically a few strokes (legs + optional base on ref line)
        if len(lines) < 2 or len(lines) > 8:
            continue
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        # Typical fillet triangle footprint (incl. small welds on dense A0)
        if not (2.2 <= w <= 22.0 and 2.2 <= h <= 22.0):
            continue
        ratio = max(w, h) / max(min(w, h), 0.1)
        # Near-square / mild rectangle — skip long chevrons / hatching
        if ratio > 3.2:
            continue
        filled = bool(d.get("fill"))
        # Prefer open stroke triangles; filled only if smallish
        if filled and not (2.8 <= max(w, h) <= 16.0):
            continue
        anchors.append(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))

    out: list[fitz.Point] = []
    for a in anchors:
        if any(math.hypot(a.x - b.x, a.y - b.y) < 4.0 for b in out):
            continue
        out.append(a)
    return out


def _weld_size_candidates(
    page: fitz.Page,
    items: list[tuple[str, fitz.Rect, float, tuple]],
) -> list[tuple[str, fitz.Rect, float]]:
    """
    Fillet / weld sizes beside a weld triangle on the reference line.
    Examples: 6 △, 3 △, 4 △, 1 △, 5 with weld-all-around ○.
    Number may sit left/right and above/below the reference line.
    """
    anchors = _fillet_weld_anchors(page)
    if not anchors:
        return []
    bound = _page_bound(page)
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        # Metric fillet sizes 1–12 (1 appears on thin welds)
        if not re.match(r"^(?:[1-9]|1[0-2])$", t):
            continue
        if rect.x0 < bound.x0 + 40 or rect.x1 > bound.x1 - 40:
            continue
        if rect.y0 < bound.y0 + 30 or rect.y1 > bound.y1 - 30:
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        for a in anchors:
            dx = cx - a.x
            dy = cy - a.y
            # Beside △ on either side of the reference line
            near_x = (-60.0 <= dx <= -1.5) or (1.5 <= dx <= 35.0)
            near_y = abs(dy) <= max(size, 8.0) * 3.8
            if near_x and near_y:
                out.append((t, rect, size))
                break
    return out


def _roughness_anchors(page: fitz.Page) -> list[fitz.Point]:
    """Small V / check surface-texture symbols (ISO 1302)."""
    anchors: list[fitz.Point] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return anchors
    for d in drawings:
        if d.get("fill"):
            continue
        items = d.get("items") or []
        lines = [it for it in items if it[0] == "l"]
        if len(lines) not in (2, 3, 4):
            continue
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if not (2.0 <= w <= 18.0 and 2.0 <= h <= 18.0):
            continue
        anchors.append(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return anchors


def _surface_finish_candidates(
    page: fitz.Page,
    items: list[tuple[str, fitz.Rect, float, tuple]],
) -> list[tuple[str, fitz.Rect, float]]:
    """
    Roughness grades next to a surface-texture V symbol.
    Accepts ISO N grades 1–4 and Ra decimals (0.8, 1.6, 3.2, 6.3…).
    """
    anchors = _roughness_anchors(page)
    if not anchors:
        return []
    bound = _page_bound(page)
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        if not (
            re.match(r"^[1-4]$", t)
            or re.match(r"^(?:0\.8|1\.6|3\.2|6\.3|12\.5|25(?:\.0)?)$", t)
        ):
            continue
        if rect.x0 < bound.x0 + 40 or rect.x1 > bound.x1 - 40:
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        for a in anchors:
            if math.hypot(cx - a.x, cy - a.y) <= max(size, 6.0) * 3.2:
                out.append((t, rect, size))
                break
    return out


def _near_fcf_box(page: fitz.Page, cx: float, cy: float, size: float) -> bool:
    """
    True when short H+V strokes around a glyph look like a GD&T frame
    (compartment box drawn as separate line segments).
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return False
    reach = max(size * 4.0, 28.0)
    h_segs = 0
    v_segs = 0
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        rcx = (r.x0 + r.x1) / 2
        rcy = (r.y0 + r.y1) / 2
        if abs(rcx - cx) > reach or abs(rcy - cy) > reach:
            continue
        w, h = float(r.width), float(r.height)
        # Thin long strokes used as FCF borders / dividers
        if w >= 12.0 and h <= 1.2:
            h_segs += 1
        elif h >= 10.0 and w <= 1.2:
            v_segs += 1
        elif 20.0 <= w <= 130.0 and 6.0 <= h <= 36.0:
            return True
        elif 6.0 <= w <= 36.0 and 20.0 <= h <= 130.0:
            return True
    return h_segs >= 2 and v_segs >= 2


def _gdt_tolerance_candidates(
    page: fitz.Page,
    items: list[tuple[str, fitz.Rect, float, tuple]],
) -> list[tuple[str, fitz.Rect, float]]:
    """
    Tolerance values (0.05, 0.2…) from feature-control frames.

    Handles:
      • merged garbled cells: 'n 0.05 I', '_ 0.05 J', 'g 0.2 A'
      • bare 0.05 / 0.2 sitting inside H/V stroke boxes
    """
    bound = page.rect
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        # Skip title-block / off-page band (Ra table noise)
        if cy < bound.y0 + 140 or cy > bound.y1 - 20:
            continue
        if cx < bound.x0 + 20 or cx > bound.x1 - 20:
            continue
        # Merged FCF row/column: symbol + tol + datum letter
        # 'n'/'I' ≈ ⊥, 'g' ≈ ∠, '_' ≈ flatness
        m = re.match(
            r"^[n_Il1g⊥∠⌯⏤\u2212\- ag]{0,2}\s*"
            r"(0\.0[1-9]|0\.[1-9]\d?)\s*"
            r"([A-ZIJ]{0,2})$",
            t,
            re.IGNORECASE)
        if m:
            val, datum = m.group(1), m.group(2)
            if val in {"0.1", "0.4", "0.025"} and not datum:
                continue
            out.append((val, rect, size))
            continue
        # Bare micro-tolerance inside a frame
        if re.match(r"^(?:0\.0[1-9]|0\.[1-9]\d?)$", t):
            if t in {"0.1", "0.4", "0.025"}:
                continue
            if _near_fcf_box(page, cx, cy, size):
                out.append((t, rect, size))
    return out

def _has_drawn_parentheses(drawings: list, rect: fitz.Rect) -> bool:
    """
    True when CAD drew '(' ')' as short polylines flanking a number
    (common for reference dims — digits extract as text, parens as vectors).

    Parentheses are often split into many tiny line drawings, so we cluster
    short strokes in left/right (or above/below) bands beside the glyph.
    """
    pad = max(8.0, min(rect.width, rect.height) * 1.1)
    search = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    left_y: list[float] = []
    right_y: list[float] = []
    above_x: list[float] = []
    below_x: list[float] = []
    left_n = right_n = above_n = below_n = 0
    tcx = (rect.x0 + rect.x1) / 2
    tcy = (rect.y0 + rect.y1) / 2
    for d in drawings:
        r = d.get("rect")
        if r is None or not search.intersects(r):
            continue
        w, h = float(r.width), float(r.height)
        if max(w, h) > max(rect.height, rect.width, 14.0) * 1.4:
            continue
        if max(w, h) < 0.8:
            continue
        rcx = (r.x0 + r.x1) / 2
        rcy = (r.y0 + r.y1) / 2
        if min(w, h) > 3.5 and max(w, h) / max(min(w, h), 0.1) < 1.4:
            continue
        if rcx <= rect.x0 + 1.0 and abs(rcy - tcy) <= max(rect.height, 10.0):
            left_y.extend([float(r.y0), float(r.y1)])
            left_n += 1
        elif rcx >= rect.x1 - 1.0 and abs(rcy - tcy) <= max(rect.height, 10.0):
            right_y.extend([float(r.y0), float(r.y1)])
            right_n += 1
        if rcy <= rect.y0 + 1.0 and abs(rcx - tcx) <= max(rect.width, 10.0):
            above_x.extend([float(r.x0), float(r.x1)])
            above_n += 1
        elif rcy >= rect.y1 - 1.0 and abs(rcx - tcx) <= max(rect.width, 10.0):
            below_x.extend([float(r.x0), float(r.x1)])
            below_n += 1

    def _span_ok(vals: list[float], need: float, n: int) -> bool:
        # Real drawn parens are several short segments spanning ~glyph size
        if n < 3 or len(vals) < 2:
            return False
        return (max(vals) - min(vals)) >= need * 0.45

    need_h = max(rect.height * 0.5, 4.0)
    need_w = max(rect.width * 0.5, 4.0)
    horiz = _span_ok(left_y, need_h, left_n) and _span_ok(right_y, need_h, right_n)
    vert = _span_ok(above_x, need_w, above_n) and _span_ok(below_x, need_w, below_n)
    return horiz or vert


def _drawn_paren_ref_candidates(
    page: fitz.Page,
    items: list[tuple[str, fitz.Rect, float, tuple]],
    drawings: list | None = None,
) -> list[tuple[str, fitz.Rect, float]]:
    """
    Reference dims where parentheses are vector-drawn: 2227 → '(2227)'.
    Covers vertical overall lengths with ( ) not present in the text layer.
    """
    if drawings is None:
        try:
            drawings = page.get_drawings()
        except Exception:
            return []
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        if not re.match(r"^\d{2,5}(?:[.,]\d+)?$", t):
            continue
        if t.startswith("("):
            continue
        if not _has_drawn_parentheses(drawings, rect):
            continue
        out.append((f"({t})", rect, size))
    return out


def _norm_dim_text(t: str) -> str:
    t = _clean(t).lower()
    # Treat (2227) and 2227 as the same callout for de-dupe
    t = re.sub(r"^\(\s*|\s*\)$", "", t)
    t = re.sub(r"\s*[x×]\s*", "x", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _level_settings(level: int) -> dict:
    """Progressive recovery settings for cross-check passes."""
    level = max(1, min(int(level), 5))
    return {
        1: {"merge_scale": 1.0, "size_lo": 3.0, "size_hi": 28.0, "title_bottom": 0.90, "title_right": 0.90},
        2: {"merge_scale": 1.6, "size_lo": 2.5, "size_hi": 32.0, "title_bottom": 0.93, "title_right": 0.92},
        3: {"merge_scale": 2.2, "size_lo": 2.0, "size_hi": 36.0, "title_bottom": 0.95, "title_right": 0.93},
        4: {"merge_scale": 2.8, "size_lo": 2.0, "size_hi": 38.0, "title_bottom": 0.96, "title_right": 0.94},
        5: {"merge_scale": 3.4, "size_lo": 1.8, "size_hi": 40.0, "title_bottom": 0.97, "title_right": 0.95},
    }[level]


def _is_same_dim(
    page: int,
    text: str,
    rect: fitz.Rect,
    pattern: str,
    existing: list[Characteristic],
    *,
    near_tol: float = 14.0,
) -> bool:
    """True if this candidate is already covered by a prior balloon.

    Adjacent *different* values (e.g. 261.5 next to 307) must stay separate —
    only suppress when the text matches, or boxes heavily overlap (split merge).
    """
    nt = _norm_dim_text(text)
    for prev in existing:
        if prev.page != page:
            continue
        same = _norm_dim_text(prev.text) == nt
        near = abs(prev.x - rect.x0) < near_tol and abs(prev.y - rect.y0) < near_tol
        # Centers of overlapping boxes (split vs merged same callout)
        pcx, pcy = prev.x + prev.width / 2, prev.y + prev.height / 2
        tcx, tcy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
        center_dist = math.hypot(pcx - tcx, pcy - tcy)
        overlap_tol = max(near_tol, min(prev.height, rect.height, prev.width, rect.width) * 1.2)
        # Same callout split into two detections
        same_callout = same and center_dist < max(near_tol * 1.5, overlap_tol)
        # Different text but essentially the same glyph box (merge artifact)
        box_overlap = center_dist < max(6.0, min(prev.width, rect.width, prev.height, rect.height) * 0.55)
        plate_dup = same and pattern == "plate_size" and prev.pattern == "plate_size"
        thk_dup = same and pattern == "material_thk" and prev.pattern == "material_thk"
        identity_dup = (
            same
            and pattern in {"drawing_no", "revision", "part_no"}
            and prev.pattern == pattern
        )
        if (same and (near or same_callout or plate_dup or thk_dup or identity_dup)) or box_overlap:
            return True
    return False


def detect_dimensions(
    doc: fitz.Document,
    *,
    level: int = 1,
    already: list[Characteristic] | None = None,
    use_zones: bool = True,
    clip: fitz.Rect | None = None,
    page_indices: list[int] | None = None,
) -> list[Characteristic]:
    """
    Detect dimension text on the drawing.
    level 1 = standard; 2–5 = wider merge / softer title-block recovery.
    already = prior balloons to skip (cross-check must not re-balloon).
    use_zones = apply BOM/notes/title/drawing policies (default on).
    clip = optional tile rect — only accept candidates whose center lies inside.
    page_indices = optional subset of 0-based page indexes to scan.
    """
    cfg = _level_settings(level)
    prior = list(already or [])
    found: list[tuple[int, str, fitz.Rect, float, str, str]] = []
    pages = page_indices if page_indices is not None else list(range(len(doc)))

    for page_index in pages:
        page = doc[page_index]
        bound = _page_bound(page)
        zones = _build_page_zones(page) if use_zones else None
        drawings, existing_circles, revision_tris = _page_geometry(page, page_index)
        raw_items = [
            (text, rect, size, direction)
            for text, rect, size, direction, *_ in _iter_text_lines(page)
        ]
        items = _merge_nearby_tol(
            _merge_stacked_glyphs(raw_items, merge_scale=cfg["merge_scale"]),
            merge_scale=cfg["merge_scale"],
        )

        for text, rect, size, _direction in items:
            candidates = [text, _clean(text.replace(" ", ""))]
            accepted = None
            category = ""
            pattern = ""
            for cand in candidates:
                if _is_dimension(cand):
                    accepted = _clean(cand)
                    _, pattern, category = _is_dimension_verbose(accepted)
                    category = category or ""
                    pattern = pattern or ""
                    break
            if accepted is None:
                continue
            # Prefer font size; bbox min-side can inflate after spatial merges
            if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                continue
            span_size = max(size, min(rect.width, rect.height))
            if span_size > 80:  # absurd merge
                continue
            if not bound.intersects(rect):
                continue
            tcx = (rect.x0 + rect.x1) / 2
            tcy = (rect.y0 + rect.y1) / 2
            if clip is not None and not (
                clip.x0 <= tcx <= clip.x1 and clip.y0 <= tcy <= clip.y1
            ):
                continue

            zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
            if zones and not _zone_allows_pattern(zone, pattern, category):
                continue

            # Human learning: skip ignored; force-accept happens in apply_learned_dimensions
            try:
                from .learning_store import match_learned as _match_learned
                _lab = _match_learned(accepted, page=page_index + 1, x=float(rect.x0), y=float(rect.y0))
                if _lab == "ignore":
                    continue
            except Exception:
                pass

            # Title-block band — never balloon (report fields come from title_block_extract OCR).
            if zone == "drawing" and clip is None:
                if _in_title_block(
                    float(rect.x0),
                    float(rect.y0),
                    bound,
                    bottom_frac=cfg["title_bottom"],
                    right_frac=cfg["title_right"],
                ):
                    continue
                if _in_title_table_band(float(rect.x0), float(rect.y0), size, bound):
                    continue

            # NEVER balloon sheet border zone / frame numbers (A B C… / 1 2 3…)
            if _is_border_grid_mark(accepted, float(rect.x0), float(rect.y0), bound):
                continue
            # NEVER balloon any number already inside an item circle
            if _is_circled_number(accepted, tcx, tcy, size, drawings, existing_circles):
                continue
            # NEVER balloon revision-delta triangle indices (△ 11, etc.)
            if _is_revision_index_text(accepted) and _is_inside_revision_triangle(
                tcx, tcy, size, drawings, revision_tris
            ):
                continue
            if _is_same_dim(page_index + 1, accepted, rect, pattern, prior):
                continue
            if not _category_allowed_for_balloon(category or "", pattern or ""):
                continue
            found.append((page_index, accepted, rect, size, category, pattern))

        # Fillet weld sizes — skip in strict dimension-only mode
        if not _strict_dimension_only():
            for text, rect, size in _weld_size_candidates(page, items):
                if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                    continue
                if not bound.intersects(rect):
                    continue
                tcx = (rect.x0 + rect.x1) / 2
                tcy = (rect.y0 + rect.y1) / 2
                if clip is not None and not (
                    clip.x0 <= tcx <= clip.x1 and clip.y0 <= tcy <= clip.y1
                ):
                    continue
                zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
                if zone in {"bom", "title"}:
                    continue
                if zone == "drawing" and _in_title_block(
                    float(rect.x0),
                    float(rect.y0),
                    bound,
                    bottom_frac=cfg["title_bottom"],
                    right_frac=cfg["title_right"],
                ):
                    continue
                if _is_border_grid_mark(text, float(rect.x0), float(rect.y0), bound):
                    continue
                if _is_same_dim(page_index + 1, text, rect, "weld_size", prior):
                    continue
                found.append((page_index, text, rect, size, "weld", "weld_size"))

        # Surface-finish — skip in strict dimension-only mode
        if not _strict_dimension_only():
            for text, rect, size in _surface_finish_candidates(page, items):
                if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                    continue
                if not bound.intersects(rect):
                    continue
                tcx = (rect.x0 + rect.x1) / 2
                tcy = (rect.y0 + rect.y1) / 2
                if clip is not None and not (
                    clip.x0 <= tcx <= clip.x1 and clip.y0 <= tcy <= clip.y1
                ):
                    continue
                zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
                if zone != "drawing":
                    continue
                if _in_title_block(
                    float(rect.x0),
                    float(rect.y0),
                    bound,
                    bottom_frac=cfg["title_bottom"],
                    right_frac=cfg["title_right"],
                ):
                    continue
                if _is_border_grid_mark(text, float(rect.x0), float(rect.y0), bound):
                    continue
                if _is_circled_number(text, tcx, tcy, size, drawings, existing_circles):
                    continue
                if _is_revision_index_text(text) and _is_inside_revision_triangle(
                    tcx, tcy, size, drawings, revision_tris
                ):
                    continue
                if _is_same_dim(page_index + 1, text, rect, "surface_finish", prior):
                    continue
                found.append((page_index, text, rect, size, "surface", "surface_finish"))

        # GD&T — skip in strict dimension-only mode
        if not _strict_dimension_only():
            for text, rect, size in _gdt_tolerance_candidates(page, items):
                if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                    continue
                if not bound.intersects(rect):
                    continue
                tcx = (rect.x0 + rect.x1) / 2
                tcy = (rect.y0 + rect.y1) / 2
                if clip is not None and not (
                    clip.x0 <= tcx <= clip.x1 and clip.y0 <= tcy <= clip.y1
                ):
                    continue
                zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
                if zone in {"bom", "title"}:
                    continue
                if _is_border_grid_mark(text, float(rect.x0), float(rect.y0), bound):
                    continue
                if _is_same_dim(page_index + 1, text, rect, "gdt_tolerance", prior):
                    continue
                found.append((page_index, text, rect, size, "gdt", "gdt_tolerance"))

        # Reference dims with vector-drawn parentheses — skip in strict mode
        if not _strict_dimension_only():
            for text, rect, size in _drawn_paren_ref_candidates(page, items, drawings):
                if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                    continue
                if not bound.intersects(rect):
                    continue
                tcx = (rect.x0 + rect.x1) / 2
                tcy = (rect.y0 + rect.y1) / 2
                if clip is not None and not (
                    clip.x0 <= tcx <= clip.x1 and clip.y0 <= tcy <= clip.y1
                ):
                    continue
                zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
                if zone in {"bom", "title"}:
                    continue
                if _is_border_grid_mark(text, float(rect.x0), float(rect.y0), bound):
                    continue
                if _is_circled_number(text.strip("()"), tcx, tcy, size, drawings, existing_circles):
                    continue
                if _is_same_dim(page_index + 1, text, rect, "ref_paren", prior):
                    continue
                found.append((page_index, text, rect, size, "reference", "ref_paren"))

    found.sort(key=lambda x: (x[0], round(x[2].y0, 1), round(x[2].x0, 1)))

    chars: list[Characteristic] = []
    working = list(prior)
    for page_i, text, rect, _size, category, pattern in found:
        if _is_same_dim(page_i + 1, text, rect, pattern, working):
            continue
        ch = Characteristic(
            balloon=len(working) + 1,
            page=page_i + 1,
            text=text,
            x=float(rect.x0),
            y=float(rect.y0),
            width=float(rect.width),
            height=float(rect.height),
            category=category or "",
            pattern=pattern or "",
            family=_pattern_family(pattern),
        )
        chars.append(ch)
        working.append(ch)
    return chars


def detect_dimensions_tiled(
    doc: fitz.Document,
    *,
    level: int = 2,
    already: list[Characteristic] | None = None,
    rows: int = 4,
    cols: int = 6,
) -> list[Characteristic]:
    """
    Slice/cutter recovery: scan overlapping drawing-zone tiles so crowded
    geometry dims missed by the full-page pass can still be accepted.
    Default grid is denser (4×6) for near-complete recall on A0/A1 sheets.
    """
    prior = list(already or [])
    added: list[Characteristic] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        zones = _build_page_zones(page)
        tiles = _drawing_tiles(zones, rows=rows, cols=cols)
        working = prior + added
        for tile in tiles:
            chunk = detect_dimensions(
                doc,
                level=level,
                already=working,
                use_zones=True,
                clip=tile,
                page_indices=[page_index],
            )
            # Keep only hits on this page whose center is in the tile / drawing
            for c in chunk:
                if c.page != page_index + 1:
                    continue
                tcx = c.x + c.width / 2
                tcy = c.y + c.height / 2
                if _zone_at(tcx, tcy, zones) != "drawing":
                    continue
                if _is_same_dim(c.page, c.text, fitz.Rect(c.x, c.y, c.x + c.width, c.y + c.height), c.pattern, working):
                    continue
                added.append(c)
                working.append(c)
    return added


def detect_dimensions_slice_cutter(
    doc: fitz.Document,
    *,
    already: list[Characteristic] | None = None,
    rows: int = 5,
    cols: int = 7,
    level: int = 3,
) -> list[Characteristic]:
    """
    Aggressive PyMuPDF slice/cutter pass: fine overlapping tiles across the
    drawing zone only. Used after multipass to chase remaining misses.
    """
    return detect_dimensions_tiled(
        doc, level=level, already=already, rows=rows, cols=cols,
    )


def _renumber(chars: list[Characteristic]) -> list[Characteristic]:
    return renumber_characteristics(chars, mode="continuous")


def renumber_characteristics(
    chars: list[Characteristic],
    *,
    mode: str = "continuous",
) -> list[Characteristic]:
    """Renumber balloons: continuous 1..N globally, or per_page 1..n on each sheet."""
    if mode == "per_page":
        from collections import defaultdict

        by_page: dict[int, list[Characteristic]] = defaultdict(list)
        for c in chars:
            by_page[int(c.page or 1)].append(c)
        out: list[Characteristic] = []
        for page in sorted(by_page):
            ordered = sorted(
                by_page[page],
                key=lambda c: (round(float(c.y), 1), round(float(c.x), 1)),
            )
            for i, c in enumerate(ordered, start=1):
                out.append(
                    Characteristic(
                        balloon=i,
                        page=c.page,
                        text=c.text,
                        x=c.x,
                        y=c.y,
                        width=c.width,
                        height=c.height,
                        category=c.category,
                        pattern=c.pattern,
                        family=c.family,
                        bx=c.bx,
                        by=c.by,
                        tx=c.tx,
                        ty=c.ty,
                        arrow_size=c.arrow_size,
                    )
                )
        return out
    out: list[Characteristic] = []
    for i, c in enumerate(chars, start=1):
        out.append(
            Characteristic(
                balloon=i,
                page=c.page,
                text=c.text,
                x=c.x,
                y=c.y,
                width=c.width,
                height=c.height,
                category=c.category,
                pattern=c.pattern,
                family=c.family,
                bx=c.bx,
                by=c.by,
                tx=c.tx,
                ty=c.ty,
                arrow_size=c.arrow_size,
            )
        )
    return out


# Per-run cache: dense CAD sheets call get_drawings() once (~2s), not per tile.
_PAGE_GEOM_CACHE: dict[tuple[int, int], tuple] = {}


def _clear_page_geom_cache() -> None:
    _PAGE_GEOM_CACHE.clear()


def _page_geometry(page: fitz.Page, page_index: int) -> tuple[list, list, list]:
    """
    Returns (drawings, item_circles, revision_tris), cached per page for one run.
    Always merges text-in-circle callouts so existing item balloons are never
    re-ballooned (integers or decimals already inside a circle).
    """
    key = (id(page.parent) if page.parent is not None else id(page), page_index)
    hit = _PAGE_GEOM_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    circles = _existing_item_balloon_circles(drawings)
    # Critical on dense CAD: pair plain numbers with round envelopes
    circles = circles + _infer_balloon_circles_from_text(page, drawings)
    tris = _existing_revision_triangles(drawings)
    _PAGE_GEOM_CACHE[key] = (drawings, circles, tris)
    return drawings, circles, tris


def _ocr_words_in_clip(
    page: fitz.Page,
    clip: fitz.Rect,
    *,
    dpi: float = 220.0,
) -> list[tuple[str, fitz.Rect, float]]:
    """
    Zoom (high-DPI render) a page clip and OCR it — fallback when vector
    text is missing/garbled. Returns (text, page-space rect, size).
    """
    tessdata = _resolve_tessdata()
    if not tessdata:
        return []
    clip = fitz.Rect(clip) & page.rect
    if clip.is_empty or clip.width < 8 or clip.height < 8:
        return []
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    try:
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        pdf_bytes = pix.pdfocr_tobytes(language="eng", tessdata=tessdata)
    except Exception:
        return []
    try:
        odoc = fitz.open("pdf", pdf_bytes)
    except Exception:
        return []
    out: list[tuple[str, fitz.Rect, float]] = []
    try:
        opage = odoc[0]
        # OCR PDF is 1pt-per-pixel of the zoomed pixmap
        sx = clip.width / max(opage.rect.width, 1e-6)
        sy = clip.height / max(opage.rect.height, 1e-6)
        for w in opage.get_text("words") or []:
            text = _clean(str(w[4]))
            if not text or not re.search(r"\d", text):
                continue
            r = fitz.Rect(
                clip.x0 + float(w[0]) * sx,
                clip.y0 + float(w[1]) * sy,
                clip.x0 + float(w[2]) * sx,
                clip.y0 + float(w[3]) * sy,
            )
            size = max(3.0, float(min(r.height, r.width)) if min(r.height, r.width) > 0 else float(r.height))
            out.append((text, r, size))
    finally:
        odoc.close()
    return out


def _weld_zoom_clips(page: fitz.Page, max_clips: int = 24) -> list[fitz.Rect]:
    """
    Tight high-zoom windows around fillet-weld symbols — catches small
    size digits (1/3/4/5/6) that vector extract or coarse tiles miss.
    """
    anchors = _fillet_weld_anchors(page)
    if not anchors or len(anchors) > 120:
        # Too noisy to micro-OCR every triangle — rely on page tiles instead
        return []
    used = [False] * len(anchors)
    clips: list[fitz.Rect] = []
    for i, a in enumerate(anchors):
        if used[i]:
            continue
        xs = [a.x]
        ys = [a.y]
        used[i] = True
        for j in range(i + 1, len(anchors)):
            if used[j]:
                continue
            if math.hypot(anchors[j].x - a.x, anchors[j].y - a.y) <= 55.0:
                used[j] = True
                xs.append(anchors[j].x)
                ys.append(anchors[j].y)
        pad_x, pad_y = 42.0, 28.0
        clip = fitz.Rect(
            min(xs) - pad_x, min(ys) - pad_y,
            max(xs) + pad_x, max(ys) + pad_y,
        ) & page.rect
        if clip.width < 12 or clip.height < 12:
            continue
        clips.append(clip)
        if len(clips) >= max_clips:
            break
    return clips


def _page_vector_text_sparse(page: fitz.Page, already_on_page: int) -> bool:
    """True when OCR zoom is likely useful (weak vector text / low balloon count)."""
    try:
        raw = (page.get_text("text") or "").strip()
    except Exception:
        raw = ""
    # Scanned / empty text layer
    if len(raw) < 40:
        return True
    # Dense vector sheets already yield many balloons — OCR is low-yield
    if already_on_page >= 80:
        return False
    digit_runs = len(re.findall(r"\d", raw))
    if digit_runs < 30 and already_on_page < 40:
        return True
    # Medium sheets: still OCR a few gap tiles
    return already_on_page < 55


def _accept_ocr_hit(
    page: fitz.Page,
    page_index: int,
    text: str,
    rect: fitz.Rect,
    size: float,
    *,
    zones: PageZones,
    bound: fitz.Rect,
    drawings: list,
    circles: list,
    tris: list,
    working: list[Characteristic],
) -> Characteristic | None:
    """Classify one OCR word into a balloon Characteristic, or None."""
    accepted = None
    category = ""
    pattern = ""
    for cand in (text, _clean(text.replace(" ", "")), _clean(text.replace("O", "0").replace("o", "0"))):
        if _is_dimension(cand):
            accepted = _clean(cand)
            _, pattern, category = _is_dimension_verbose(accepted)
            break
    if accepted is None and re.match(r"^(?:[1-9]|1[0-2])$", _clean(text)):
        weld_hits = _weld_size_candidates(
            page, [(text, rect, size, (1.0, 0.0))],
        )
        if weld_hits:
            accepted = _clean(text)
            pattern, category = "weld_size", "weld"
    if accepted is None:
        return None
    if not (2.0 <= size <= 40.0):
        return None
    tcx = (rect.x0 + rect.x1) / 2
    tcy = (rect.y0 + rect.y1) / 2
    zone = _zone_at(tcx, tcy, zones)
    if not _zone_allows_pattern(zone, pattern or "", category or ""):
        return None
    if _is_border_grid_mark(accepted, float(rect.x0), float(rect.y0), bound):
        return None
    if pattern != "weld_size" and _is_circled_number(
        accepted, tcx, tcy, size, drawings, circles
    ):
        return None
    if _is_revision_index_text(accepted) and _is_inside_revision_triangle(
        tcx, tcy, size, drawings, tris
    ):
        return None
    if _is_same_dim(page_index + 1, accepted, rect, pattern or "ocr", working):
        return None
    return Characteristic(
        balloon=len(working) + 1,
        page=page_index + 1,
        text=accepted,
        x=float(rect.x0),
        y=float(rect.y0),
        width=float(rect.width),
        height=float(rect.height),
        category=category or "ocr",
        pattern=pattern or "ocr_tile",
        family=_pattern_family(pattern),
    )


def detect_dimensions_ocr_fallback(
    doc: fitz.Document,
    already: list[Characteristic] | None = None,
    *,
    rows: int = 3,
    cols: int = 4,
    dpi: float = 220.0,
    max_tiles: int = 14,
) -> list[Characteristic]:
    """
    Zoomed-tile OCR fallback: render under-ballooned regions at high DPI,
    OCR them, and accept dimension-like hits PyMuPDF vector text missed.
    Also micro-zooms fillet-weld clusters for small size digits.
    """
    if not ocr_available():
        return []

    prior = list(already or [])
    added: list[Characteristic] = []
    tiles_done = 0
    for page_index in range(len(doc)):
        if tiles_done >= max_tiles:
            break
        page = doc[page_index]
        zones = _build_page_zones(page)
        bound = _page_bound(page)
        drawings, circles, tris = _page_geometry(page, page_index)
        working = prior + added
        on_page = sum(1 for c in working if c.page == page_index + 1)
        sparse = _page_vector_text_sparse(page, on_page)

        # 1) Micro-zoom weld symbols when vector looks incomplete
        weld_budget = 10 if sparse else 4
        for wclip in _weld_zoom_clips(page, max_clips=weld_budget):
            if tiles_done >= max_tiles:
                break
            tiles_done += 1
            try:
                words = _ocr_words_in_clip(page, wclip, dpi=max(dpi, 300.0))
            except Exception:
                continue
            for text, rect, size in words:
                ch = _accept_ocr_hit(
                    page, page_index, text, rect, size,
                    zones=zones, bound=bound, drawings=drawings,
                    circles=circles, tris=tris, working=working,
                )
                if ch is None:
                    continue
                added.append(ch)
                working.append(ch)

        # 2) Coarse tiles only when page still looks under-ballooned
        if not sparse and on_page >= 60:
            continue

        tiles = _drawing_tiles(zones, rows=rows, cols=cols)
        if not tiles:
            tw, th = bound.width / cols, bound.height / rows
            for ri in range(rows):
                for ci in range(cols):
                    tiles.append(fitz.Rect(
                        bound.x0 + ci * tw, bound.y0 + ri * th,
                        bound.x0 + (ci + 1) * tw, bound.y0 + (ri + 1) * th,
                    ) & bound)

        scored: list[tuple[int, fitz.Rect]] = []
        for tile in tiles:
            existing_here = sum(
                1 for c in working
                if c.page == page_index + 1
                and tile.x0 <= c.x + c.width / 2 <= tile.x1
                and tile.y0 <= c.y + c.height / 2 <= tile.y1
            )
            if existing_here >= 10:
                continue
            scored.append((existing_here, tile))
        scored.sort(key=lambda x: x[0])

        for _count, tile in scored:
            if tiles_done >= max_tiles:
                break
            tiles_done += 1
            try:
                words = _ocr_words_in_clip(page, tile, dpi=dpi)
            except Exception:
                continue
            for text, rect, size in words:
                ch = _accept_ocr_hit(
                    page, page_index, text, rect, size,
                    zones=zones, bound=bound, drawings=drawings,
                    circles=circles, tris=tris, working=working,
                )
                if ch is None:
                    continue
                added.append(ch)
                working.append(ch)
    return added


def detect_dimensions_multipass(
    doc: fitz.Document,
    passes: int = 1,
    *,
    start_level: int = 1,
    already: list[Characteristic] | None = None,
    use_zones: bool = True,
    use_tiles: bool = True,
    use_ocr: bool = True,
) -> tuple[list[Characteristic], list[dict]]:
    """
    Run detection up to `passes` times with rising recovery levels.
    Pass 1 = standard (zone-aware); later passes only add newly recovered dims.
    After tiles/slice, optional zoomed OCR fallback for missed vector text.
    """
    passes = max(1, min(int(passes), 5))
    start_level = max(1, min(int(start_level), 5))
    accumulated = list(already or [])
    log: list[dict] = []
    _clear_page_geom_cache()

    for i in range(passes):
        level = min(5, start_level + i)
        added = detect_dimensions(
            doc, level=level, already=accumulated, use_zones=use_zones,
        )
        accumulated = _renumber(accumulated + added)
        entry = {
            "pass": i + 1,
            "level": level,
            "added": len(added),
            "total": len(accumulated),
            "mode": "full",
        }
        log.append(entry)

        # After each recovery pass (and once after pass 1), tile the drawing zone
        if use_tiles and (i == 0 or len(added) > 0):
            # Dense vector sheets: full-page pass already finds most dims. Running
            # 24+ tile clips on large A0/A1 pages can take 7–15+ minutes while
            # adding only a few extras — skip to keep ballooning responsive.
            if i == 0 and len(accumulated) >= 65:
                log.append({
                    "pass": i + 1,
                    "level": 0,
                    "added": 0,
                    "total": len(accumulated),
                    "mode": "tiles_skipped_dense",
                })
            else:
                tile_level = min(5, level + 1)
                # Denser virtual-zoom tiles (clip regions) — improves recall on
                # crowded vector sheets without raster OCR.
                rows, cols = (4, 6) if i == 0 else (5, 7)
                tiled = detect_dimensions_tiled(
                    doc, level=tile_level, already=accumulated, rows=rows, cols=cols,
                )
                if tiled:
                    accumulated = _renumber(accumulated + tiled)
                    log.append({
                        "pass": i + 1,
                        "level": tile_level,
                        "added": len(tiled),
                        "total": len(accumulated),
                        "mode": "tiles",
                    })

        # After the opening pass, stop when recovery finds nothing new —
        # but always finish with one dense slice/cutter sweep first.
        if i > 0 and len(added) == 0:
            if use_tiles:
                sliced = detect_dimensions_slice_cutter(
                    doc, already=accumulated, rows=5, cols=7, level=min(5, level + 1),
                )
                if sliced:
                    accumulated = _renumber(accumulated + sliced)
                    log.append({
                        "pass": i + 1,
                        "level": min(5, level + 1),
                        "added": len(sliced),
                        "total": len(accumulated),
                        "mode": "slice_cutter",
                    })
            break

        # Final slice/cutter sweep on last planned pass
        if use_tiles and i == passes - 1 and len(accumulated) < 65:
            sliced = detect_dimensions_slice_cutter(
                doc, already=accumulated, rows=5, cols=7, level=min(5, level + 1),
            )
            if sliced:
                accumulated = _renumber(accumulated + sliced)
                log.append({
                    "pass": i + 1,
                    "level": min(5, level + 1),
                    "added": len(sliced),
                    "total": len(accumulated),
                    "mode": "slice_cutter",
                })

        if already is not None and len(added) == 0 and i > 0:
            break

    # Zoomed OCR fallback when vector text is weak (needs Tesseract tessdata)
    if use_ocr and ocr_available():
        try:
            ocr_hits = detect_dimensions_ocr_fallback(
                doc, already=accumulated, rows=3, cols=4, dpi=220.0, max_tiles=14,
            )
            if ocr_hits:
                accumulated = _renumber(accumulated + ocr_hits)
                log.append({
                    "pass": len(log) + 1,
                    "level": 0,
                    "added": len(ocr_hits),
                    "total": len(accumulated),
                    "mode": "ocr_zoom",
                })
            else:
                log.append({
                    "pass": len(log) + 1,
                    "level": 0,
                    "added": 0,
                    "total": len(accumulated),
                    "mode": "ocr_zoom",
                })
        except Exception:
            pass
    elif use_ocr and not ocr_available():
        log.append({
            "pass": len(log) + 1,
            "level": 0,
            "added": 0,
            "total": len(accumulated),
            "mode": "ocr_unavailable",
        })

    return accumulated, log


def collect_miss_candidates(
    doc: fitz.Document,
    already: list[Characteristic],
    *,
    max_per_page: int = 80,
) -> list[dict]:
    """
    Vector spans with digits that were not ballooned — for AI / HITL review.
    Still skips border marks and numbers inside existing item circles.
    """
    out: list[dict] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        bound = _page_bound(page)
        zones = _build_page_zones(page)
        drawings, circles, _tris = _page_geometry(page, page_index)
        raw = [
            (text, rect, size, direction)
            for text, rect, size, direction, *_ in _iter_text_lines(page)
        ]
        items = _merge_nearby_tol(_merge_stacked_glyphs(raw, merge_scale=1.6), merge_scale=1.6)
        page_hits = 0
        for text, rect, size, _direction in items:
            if page_hits >= max_per_page:
                break
            t = _clean(text)
            if not t or not re.search(r"\d", t):
                continue
            if len(t) > 48:
                continue
            tcx = (rect.x0 + rect.x1) / 2
            tcy = (rect.y0 + rect.y1) / 2
            if _is_border_grid_mark(t, float(rect.x0), float(rect.y0), bound):
                continue
            if _is_circled_number(t, tcx, tcy, size, drawings, circles):
                continue
            if _is_same_dim(page_index + 1, t, rect, "candidate", already):
                continue
            zone = _zone_at(tcx, tcy, zones)
            if _strict_dimension_only():
                if zone != "drawing":
                    continue
                if _in_title_block(float(rect.x0), float(rect.y0), bound):
                    continue
                if _in_title_table_band(float(rect.x0), float(rect.y0), size, bound):
                    continue
            reason = _exclusion_reason(t) or ""
            matched, pattern, category = _is_dimension_verbose(t)
            if matched and _zone_allows_pattern(zone, pattern or "", category or ""):
                # Would normally be accepted — skip unless zone/title filtered
                if zone == "drawing" and not _in_title_block(float(rect.x0), float(rect.y0), bound):
                    if not _in_title_table_band(float(rect.x0), float(rect.y0), size, bound):
                        continue
            # Neighbors: nearby short texts for AI context
            neighbors: list[str] = []
            for t2, r2, *_ in items:
                if t2 == text:
                    continue
                ncx = (r2.x0 + r2.x1) / 2
                ncy = (r2.y0 + r2.y1) / 2
                if math.hypot(ncx - tcx, ncy - tcy) < 55 and len(neighbors) < 4:
                    neighbors.append(_clean(t2)[:40])
            out.append({
                "id": len(out),
                "page": page_index + 1,
                "text": t,
                "x": float(rect.x0),
                "y": float(rect.y0),
                "width": float(rect.width),
                "height": float(rect.height),
                "size": float(size),
                "zone": zone,
                "reason": reason or ("no_pattern" if not matched else f"filtered:{pattern}"),
                "neighbors": " | ".join(neighbors),
                "pattern": pattern or "",
                "category": category or "",
            })
            page_hits += 1
    return out


def find_unballooned_dimensions(
    doc: fitz.Document,
    already: list[Characteristic],
    *,
    level: int = 4,
    use_zones: bool = True,
    max_items: int = 500,
) -> list[dict]:
    """
    Audit: find dimension-like spans that are not covered by current balloons.
    Uses a recovery-level scan but does not place balloons — returns candidates only.
    """
    cfg = _level_settings(level)
    missing: list[dict] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        bound = _page_bound(page)
        zones = _build_page_zones(page) if use_zones else None
        drawings, circles, _tris = _page_geometry(page, page_index)
        raw_items = [
            (text, rect, size, direction)
            for text, rect, size, direction, *_ in _iter_text_lines(page)
        ]
        items = _merge_nearby_tol(
            _merge_stacked_glyphs(raw_items, merge_scale=cfg["merge_scale"]),
            merge_scale=cfg["merge_scale"],
        )
        for text, rect, size, _direction in items:
            if len(missing) >= max_items:
                return missing
            accepted = None
            category = ""
            pattern = ""
            for cand in (text, _clean(text.replace(" ", ""))):
                if _is_dimension(cand):
                    accepted = _clean(cand)
                    _, pattern, category = _is_dimension_verbose(accepted)
                    category = category or ""
                    pattern = pattern or ""
                    break
            if accepted is None:
                continue
            if not (cfg["size_lo"] <= size <= cfg["size_hi"]):
                continue
            tcx = (rect.x0 + rect.x1) / 2
            tcy = (rect.y0 + rect.y1) / 2
            zone = _zone_at(tcx, tcy, zones) if zones else "drawing"
            if zones and not _zone_allows_pattern(zone, pattern, category):
                continue
            if _is_border_grid_mark(accepted, float(rect.x0), float(rect.y0), bound):
                continue
            if _is_circled_number(accepted, tcx, tcy, size, drawings, circles):
                continue
            if _is_same_dim(page_index + 1, accepted, rect, category or "dim", already):
                continue
            missing.append({
                "id": len(missing),
                "page": page_index + 1,
                "text": accepted,
                "x": float(rect.x0),
                "y": float(rect.y0),
                "width": float(rect.width),
                "height": float(rect.height),
                "size": float(size),
                "zone": zone,
                "pattern": pattern,
                "category": category,
                "reason": "not_ballooned",
            })
    return missing


def apply_learned_dimensions(
    doc: fitz.Document,
    already: list[Characteristic],
) -> list[Characteristic]:
    """Accept spans the human marked as dimensions previously."""
    try:
        from .learning_store import match_learned
    except ImportError:
        return []
    added: list[Characteristic] = []
    working = list(already)
    for page_index in range(len(doc)):
        page = doc[page_index]
        bound = _page_bound(page)
        drawings, circles, _tris = _page_geometry(page, page_index)
        raw = [
            (text, rect, size, direction)
            for text, rect, size, direction, *_ in _iter_text_lines(page)
        ]
        items = _merge_nearby_tol(_merge_stacked_glyphs(raw), merge_scale=1.0)
        for text, rect, size, _direction in items:
            t = _clean(text)
            if not t or not re.search(r"\d", t):
                continue
            tcx = (rect.x0 + rect.x1) / 2
            tcy = (rect.y0 + rect.y1) / 2
            label = match_learned(t, page=page_index + 1, x=float(rect.x0), y=float(rect.y0))
            if label != "dimension":
                continue
            if _is_border_grid_mark(t, float(rect.x0), float(rect.y0), bound):
                continue
            if _is_circled_number(t, tcx, tcy, size, drawings, circles):
                continue
            if _is_same_dim(page_index + 1, t, rect, "learned", working):
                continue
            # Respect explicit ignore with higher priority when both exist — match_learned returns best
            ch = Characteristic(
                balloon=len(working) + 1,
                page=page_index + 1,
                text=t,
                x=float(rect.x0),
                y=float(rect.y0),
                width=float(rect.width),
                height=float(rect.height),
                category="learned",
                pattern="learned",
                family="generic",
            )
            added.append(ch)
            working.append(ch)
    return added


def apply_ai_fallback(
    doc: fitz.Document,
    already: list[Characteristic],
    *,
    provider: str,
    api_key: str | None = None,
    model: str | None = None,
    min_confidence: float = 0.72,
    max_candidates: int = 120,
    learn_ai_hits: bool = True,
) -> tuple[list[Characteristic], list[dict]]:
    """
    Classify rejected vector spans with OpenAI/Claude; balloon high-confidence dims.
    Optionally write AI accepts into the learning store (weak count=1).
    """
    from .ai_fallback import classify_candidates
    from .learning_store import add_feedback, few_shot_examples, match_learned

    cands = collect_miss_candidates(doc, already)
    # Drop spans humans already marked ignore
    filtered = []
    for c in cands:
        learned = match_learned(c["text"], page=c["page"], x=c["x"], y=c["y"])
        if learned == "ignore":
            continue
        if learned == "dimension":
            continue  # handled by apply_learned_dimensions
        filtered.append(c)
    filtered = filtered[:max_candidates]
    if not filtered:
        return [], []

    # Re-id for the model
    for i, c in enumerate(filtered):
        c["id"] = i

    results = classify_candidates(
        filtered,
        provider=provider,
        api_key=api_key,
        model=model,
        few_shot=few_shot_examples(16),
    )
    added: list[Characteristic] = []
    working = list(already)
    trail: list[dict] = []
    for r in results:
        idx = int(r["id"])
        if idx < 0 or idx >= len(filtered):
            continue
        c = filtered[idx]
        label = r.get("label")
        conf = float(r.get("confidence") or 0)
        trail.append({
            "text": c["text"],
            "page": c["page"],
            "label": label,
            "confidence": conf,
            "zone": c.get("zone"),
            "reason": c.get("reason"),
        })
        if label != "dimension" or conf < min_confidence:
            continue
        zone = str(c.get("zone") or "drawing")
        if _strict_dimension_only():
            if zone != "drawing":
                continue
            _matched, _pat, _cat = _is_dimension_verbose(c["text"])
            if not _category_allowed_for_balloon(_cat or "", _pat or ""):
                continue
        rect = fitz.Rect(c["x"], c["y"], c["x"] + c["width"], c["y"] + c["height"])
        if _is_same_dim(c["page"], c["text"], rect, "ai_fallback", working):
            continue
        ch = Characteristic(
            balloon=len(working) + 1,
            page=int(c["page"]),
            text=c["text"],
            x=float(c["x"]),
            y=float(c["y"]),
            width=float(c["width"]),
            height=float(c["height"]),
            category="ai",
            pattern="ai_fallback",
            family="generic",
        )
        added.append(ch)
        working.append(ch)
        if learn_ai_hits:
            try:
                add_feedback(
                    text=c["text"],
                    label="dimension",
                    page=c["page"],
                    x=c["x"],
                    y=c["y"],
                    width=c["width"],
                    height=c["height"],
                    neighbors=c.get("neighbors") or "",
                    source="ai_confirm",
                )
            except Exception:
                pass
    return added, trail


def balloon_pdf(
    pdf_bytes: bytes,
    passes: int = 3,
    *,
    use_zones: bool = True,
    use_tiles: bool = True,
    use_learning: bool = True,
    ai_fallback: bool = False,
    ai_provider: str = "openai",
    ai_api_key: str | None = None,
    ai_model: str | None = None,
    use_ocr: bool | None = None,
    numbering_mode: str = "continuous",
) -> tuple[bytes, list[Characteristic], list[dict]]:
    """
    Balloon a drawing PDF.
    Order: vector multipass + slice/cutter → learned feedback → optional AI.
    Default passes=3 for high recall on dense fab drawings.
    """
    import os

    # Enable AI fallback via env when no explicit flag (needs API key)
    if not ai_fallback:
        ai_fallback = os.environ.get("SMORX_AI_FALLBACK", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if ai_fallback and not (ai_api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
            ai_fallback = False
    if ai_fallback and not ai_provider:
        ai_provider = "openai" if os.environ.get("OPENAI_API_KEY") else ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "openai")
    if use_ocr is None:
        use_ocr = os.environ.get("SMORX_OCR_FALLBACK", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        chars, pass_log = detect_dimensions_multipass(
            src,
            passes=passes,
            use_zones=use_zones,
            use_tiles=use_tiles,
            use_ocr=bool(use_ocr),
        )
        mode = "per_page" if str(numbering_mode).strip().lower() == "per_page" else "continuous"
        chars = renumber_characteristics(chars, mode=mode)
        if use_learning:
            learned = apply_learned_dimensions(src, chars)
            if learned:
                learned = [
                    c for c in learned
                    if _category_allowed_for_balloon(c.category or "", c.pattern or "")
                ]
                chars = renumber_characteristics(chars + learned, mode=mode)
                pass_log.append({
                    "pass": len(pass_log) + 1,
                    "level": 0,
                    "added": len(learned),
                    "total": len(chars),
                    "mode": "learned",
                })
        if ai_fallback:
            ai_chars, _trail = apply_ai_fallback(
                src,
                chars,
                provider=ai_provider,
                api_key=ai_api_key,
                model=ai_model,
            )
            if ai_chars:
                ai_chars = [
                    c for c in ai_chars
                    if _category_allowed_for_balloon(c.category or "", c.pattern or "")
                ]
                chars = renumber_characteristics(chars + ai_chars, mode=mode)
                pass_log.append({
                    "pass": len(pass_log) + 1,
                    "level": 0,
                    "added": len(ai_chars),
                    "total": len(chars),
                    "mode": f"ai:{ai_provider}",
                })
        ballooned = draw_balloons(src, chars)
        try:
            out_bytes = ballooned.tobytes(deflate=True, garbage=3)
        finally:
            ballooned.close()
        return out_bytes, chars, pass_log
    finally:
        src.close()


def balloon_pdf_crosscheck(
    pdf_bytes: bytes,
    existing: list[Characteristic],
    rounds: int = 1,
    *,
    start_level: int = 2,
    use_zones: bool = True,
    use_tiles: bool = True,
    use_learning: bool = True,
    ai_fallback: bool = False,
    ai_provider: str = "openai",
    ai_api_key: str | None = None,
    ai_model: str | None = None,
) -> tuple[bytes, list[Characteristic], list[dict]]:
    """Extra cross-check on an already-ballooned result."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        chars, pass_log = detect_dimensions_multipass(
            src,
            passes=rounds,
            start_level=start_level,
            already=existing,
            use_zones=use_zones,
            use_tiles=use_tiles,
        )
        if use_learning:
            learned = apply_learned_dimensions(src, chars)
            if learned:
                chars = _renumber(chars + learned)
                pass_log.append({
                    "pass": len(pass_log) + 1,
                    "level": 0,
                    "added": len(learned),
                    "total": len(chars),
                    "mode": "learned",
                })
        if ai_fallback:
            ai_chars, _trail = apply_ai_fallback(
                src,
                chars,
                provider=ai_provider,
                api_key=ai_api_key,
                model=ai_model,
            )
            if ai_chars:
                chars = _renumber(chars + ai_chars)
                pass_log.append({
                    "pass": len(pass_log) + 1,
                    "level": 0,
                    "added": len(ai_chars),
                    "total": len(chars),
                    "mode": f"ai:{ai_provider}",
                })
        ballooned = draw_balloons(src, chars)
        try:
            out_bytes = ballooned.tobytes(deflate=True, garbage=3)
        finally:
            ballooned.close()
        return out_bytes, chars, pass_log
    finally:
        src.close()


def redraw_balloons(pdf_bytes: bytes, chars: list[Characteristic]) -> bytes:
    """Re-draw balloons for an updated characteristic list (HITL manual adds)."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        # Preserve existing balloon numbers / bx,by — do not spatial-renumber.
        ballooned = draw_balloons(src, list(chars))
        try:
            return ballooned.tobytes(deflate=True, garbage=3)
        finally:
            ballooned.close()
    finally:
        src.close()


def add_manual_characteristic(
    chars: list[Characteristic],
    *,
    page: int,
    text: str,
    x: float,
    y: float,
    width: float = 20.0,
    height: float = 12.0,
) -> list[Characteristic]:
    """Append a human-added dimension and renumber."""
    ch = Characteristic(
        balloon=len(chars) + 1,
        page=int(page),
        text=_clean(text),
        x=float(x),
        y=float(y),
        width=float(width),
        height=float(height),
        category="manual",
        pattern="manual",
        family="generic",
    )
    return _renumber(list(chars) + [ch])


# =====================================================
# BALLOON GEOMETRY
# =====================================================

def _balloon_radius(page: fitz.Page, label: str = "1") -> float:
    """Visible balloon on A3/A4 — large enough to read when zoomed to page width."""
    bound = _page_bound(page)
    short = min(bound.width, bound.height)
    # ~10–14 pt on A3; was ~5.5 and nearly invisible at fit-page zoom
    base = max(10.0, min(14.0, short * 0.012))
    if len(label) >= 4:
        return base * 1.22
    if len(label) == 3:
        return base * 1.10
    return base


def _arrowhead(
    tip: fitz.Point,
    toward: fitz.Point,
    size: float,
) -> tuple[fitz.Point, fitz.Point, fitz.Point]:
    """Filled triangle arrowhead at tip, pointing along tip←toward."""
    dx = tip.x - toward.x
    dy = tip.y - toward.y
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    # base center sits behind the tip
    bx = tip.x - ux * size
    by = tip.y - uy * size
    px, py = -uy, ux  # perpendicular
    left = fitz.Point(bx + px * size * 0.45, by + py * size * 0.45)
    right = fitz.Point(bx - px * size * 0.45, by - py * size * 0.45)
    return tip, left, right


def _vertical_dim_line_side(
    ax: float,
    ay: float,
    text_h: float,
    drawings: list,
) -> int:
    """
    Which side of vertical dimension text the dimension line sits on.
    Returns -1 (left), +1 (right), or 0 (unknown).
    Balloons should go on the OPPOSITE side so the line stays visible.
    """
    reach_x = max(text_h * 3.0, 36.0)
    left_score = 0.0
    right_score = 0.0
    y_pad = max(text_h * 2.2, 20.0)
    for d in drawings:
        for it in d.get("items") or []:
            if it[0] != "l":
                continue
            p1, p2 = it[1], it[2]
            x1, y1 = float(p1.x), float(p1.y)
            x2, y2 = float(p2.x), float(p2.y)
            dx, dy = x2 - x1, y2 - y1
            # Nearly vertical stroke
            if abs(dx) > abs(dy) * 0.28:
                continue
            length = math.hypot(dx, dy)
            if length < max(text_h * 0.7, 10.0):
                continue
            mx = (x1 + x2) / 2
            ymin, ymax = min(y1, y2), max(y1, y2)
            if ymax < ay - y_pad or ymin > ay + y_pad:
                continue
            if abs(mx - ax) > reach_x or abs(mx - ax) < 1.0:
                continue
            weight = length / max(abs(mx - ax), 2.0)
            if mx < ax:
                left_score += weight
            else:
                right_score += weight
    if left_score > right_score * 1.1:
        return -1
    if right_score > left_score * 1.1:
        return 1
    return 0


def _spread_balloons(
    items: list[Characteristic],
    page: fitz.Page,
) -> list[tuple[Characteristic, float, float, float]]:
    """
    Place small balloons near text with room for a short leader.
    Rules:
      - never on top of an existing item-balloon circle
      - never overlapping another new balloon
      - leader must not cross through other balloons
      - vertical dims: place on the OPPOSITE side of the dimension line
        (not on the line) and stagger when several are stacked
      - skip text that is already a number inside an item circle
    """
    bound = _page_bound(page)
    drawings, existing, revision_tris = _page_geometry(page, page.number)
    placed: list[tuple[Characteristic, float, float, float]] = []

    def _all_obstacles(
        extra: list[tuple[float, float, float]] | None = None,
    ) -> list[tuple[float, float, float]]:
        obs = list(existing)
        for _, px, py, pr in placed:
            obs.append((px, py, pr))
        if extra:
            obs.extend(extra)
        return obs

    def _balloon_hits(cx: float, cy: float, r: float) -> bool:
        for ocx, ocy, orad in _all_obstacles():
            if math.hypot(cx - ocx, cy - ocy) < (r + orad) * 1.45:
                return True
        return False

    def _stack_push(ax: float, ay: float, prefer: int, r: float) -> float:
        """Small extra offset when other balloons already sit nearby (capped)."""
        push = 0.0
        for _, px, py, pr in placed:
            if abs(py - ay) > max(r, pr) * 3.5:
                continue
            if prefer > 0 and px >= ax - r:
                push = max(push, min((px - ax) + pr * 1.15 - r * 2.2, r * 1.8))
            if prefer < 0 and px <= ax + r:
                push = max(push, min((ax - px) + pr * 1.15 - r * 2.2, r * 1.8))
        return max(0.0, push)

    def _candidate_positions(
        ax: float,
        ay: float,
        r: float,
        vertical: bool,
        prefer: int,
        stack_extra: float,
    ) -> list[tuple[float, float]]:
        """
        Short leaders only (~0.3–0.7 in). Vertical dims sit opposite the
        dim line; congested stacks stagger in Y instead of long X leaders.
        """
        positions: list[tuple[float, float]] = []
        # Cap leader length in PDF points (72 pt ≈ 1 in)
        short = [r * 2.4, r * 3.0, r * 3.6, r * 4.2]  # ~0.35–0.65 in
        medium = [r * 4.8, r * 5.6, r * 6.4]  # up to ~0.9 in last resort

        if vertical:
            for dist0 in short:
                dist = min(dist0 + stack_extra, r * 5.5)
                positions.append((ax + prefer * dist, ay))
                positions.append((ax + prefer * dist, ay - r * 1.1))
                positions.append((ax + prefer * dist, ay + r * 1.1))
            # Mild Y stagger with same short X (for stacked dims)
            for dist0 in short[1:]:
                dist = min(dist0 + stack_extra, r * 5.5)
                positions.append((ax + prefer * dist, ay - r * 2.0))
                positions.append((ax + prefer * dist, ay + r * 2.0))
            # Other side, still short
            for dist0 in medium:
                dist = min(dist0, r * 6.4)
                positions.append((ax - prefer * dist, ay))
                positions.append((ax - prefer * dist, ay - r * 1.1))
                positions.append((ax - prefer * dist, ay + r * 1.1))
            return positions

        # Horizontal dimension text — short leaders
        for dist in short + medium:
            for deg in [180, 210, 150, 240, 120, 270, 90, 300, 60, 0]:
                rad = math.radians(deg)
                positions.append((ax + math.cos(rad) * dist, ay - math.sin(rad) * dist))
        return positions

    for c in items:
        label = str(c.balloon)
        r = _balloon_radius(page, label)
        ax = c.x + c.width / 2.0
        ay = c.y + c.height / 2.0
        vertical = c.height > c.width * 1.3

        # NEVER balloon a number already inside an item circle
        if _is_circled_number(c.text, ax, ay, max(c.height, 6.0), drawings, existing):
            continue

        # Revision delta triangle indices — never balloon
        if _is_revision_index_text(c.text) and _is_inside_revision_triangle(
            ax, ay, max(c.height, 6.0), drawings, revision_tris
        ):
            continue

        # Vertical: place OPPOSITE the dimension line (line left → balloon right, etc.)
        if vertical:
            line_side = _vertical_dim_line_side(ax, ay, max(c.height, 8.0), drawings)
            prefer = -1 if line_side >= 0 else 1  # opposite of line (default: left of text)
            stack_extra = _stack_push(ax, ay, prefer, r)
        else:
            prefer = -1
            stack_extra = 0.0

        chosen: tuple[float, float] | None = None
        # Short leaders: ~0.35–0.9 in preferred (readable, not multi-inch)
        min_leader = r * (2.0 if vertical else 1.7)
        max_leader = r * 6.8
        for cx, cy in _candidate_positions(ax, ay, r, vertical, prefer, stack_extra):
            cx = max(bound.x0 + r + 2, min(cx, bound.x1 - r - 2))
            cy = max(bound.y0 + r + 2, min(cy, bound.y1 - r - 2))
            lead = math.hypot(cx - ax, cy - ay)
            if lead < min_leader or lead > max_leader:
                continue
            if _balloon_hits(cx, cy, r):
                continue
            if not _leader_clear(cx, cy, r, ax, ay, _all_obstacles()):
                continue
            # Vertical: still keep off the dim-line corridor
            if vertical and abs(cx - ax) < r * 1.8:
                continue
            chosen = (cx, cy)
            break

        # Last resort: always place — never silently drop a detected dim
        if chosen is None:
            for side in (prefer, -prefer, 1, -1):
                for dist in (r * 4.0, r * 5.2, r * 6.2, r * 7.5, r * 9.0):
                    for y_off in (0.0, -r * 1.2, r * 1.2, -r * 2.4, r * 2.4):
                        cx = ax + side * dist
                        cy = ay + y_off
                        cx = max(bound.x0 + r + 2, min(cx, bound.x1 - r - 2))
                        cy = max(bound.y0 + r + 2, min(cy, bound.y1 - r - 2))
                        if _balloon_hits(cx, cy, r * 0.85):
                            continue
                        chosen = (cx, cy)
                        break
                    if chosen is not None:
                        break
                if chosen is not None:
                    break
        if chosen is None:
            # Absolute fallback: offset left of text even if crowded
            chosen = (
                max(bound.x0 + r + 2, ax - r * 5.0),
                max(bound.y0 + r + 2, min(ay, bound.y1 - r - 2)),
            )

        placed.append((c, chosen[0], chosen[1], r))
    return placed


def draw_balloons(
    doc: fitz.Document,
    characteristics: list[Characteristic],
    *,
    color: tuple[float, float, float] = (0.85, 0.05, 0.05),
    fill: tuple[float, float, float] = (1.0, 0.97, 0.75),
) -> fitz.Document:
    """
    Small high-contrast balloons with a leader + arrow pointing at each dimension.

    When a characteristic already has bx/by (circle centre), that position is kept
    so edits/zoom round-trips do not reshuffle leaders. Fresh balloons without
    bx/by still go through placement. Mutates characteristics in place with bx/by.
    """
    out = fitz.open()
    out.insert_pdf(doc)

    by_page: dict[int, list[Characteristic]] = {}
    for c in characteristics:
        by_page.setdefault(c.page - 1, []).append(c)

    for page_i, items in by_page.items():
        page = out[page_i]
        shape = page.new_shape()

        pinned = [c for c in items if c.bx is not None and c.by is not None]
        free = [c for c in items if c.bx is None or c.by is None]
        placed: list[tuple[Characteristic, float, float, float]] = []
        for c in pinned:
            r = _balloon_radius(page, str(c.balloon))
            placed.append((c, float(c.bx), float(c.by), r))
        if free:
            placed.extend(_spread_balloons(free, page))

        for c, cx, cy, r in placed:
            c.bx = cx
            c.by = cy
            # Dimension target (leader tip) — centre of dotted-square dim box; persist for grid refs
            if c.tx is None:
                c.tx = c.x + c.width / 2.0
            if c.ty is None:
                c.ty = c.y + c.height / 2.0
            tx = float(c.tx)
            ty = float(c.ty)
            arrow_scale = float(c.arrow_size) if c.arrow_size is not None else 1.0

            # Leader starts at balloon rim toward the text
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dist, dy / dist
            # Keep a short gap so arrow doesn't sit under the number
            tip_gap = max(2.0, min(c.width, c.height) * 0.15 + 1.5)
            start = fitz.Point(cx + ux * (r + 0.4), cy + uy * (r + 0.4))
            tip = fitz.Point(tx - ux * tip_gap, ty - uy * tip_gap)

            # Leader line
            shape.draw_line(start, tip)
            shape.finish(color=color, width=0.9, stroke_opacity=1)

            # Arrowhead
            tip_p, left, right = _arrowhead(tip, start, size=max(2.2, r * 0.38 * arrow_scale))
            shape.draw_polyline([left, tip_p, right])
            shape.finish(color=color, fill=color, width=0.4, closePath=True)

            # Balloon circle (white-yellow fill, strong red rim)
            shape.draw_circle(fitz.Point(cx, cy), r)
            shape.finish(color=color, fill=fill, width=1.35)

            # Number centered in balloon
            label = str(c.balloon)
            if len(label) >= 4:
                fontsize = r * 0.95
            elif len(label) == 3:
                fontsize = r * 1.05
            else:
                fontsize = r * 1.25
            tw = fitz.get_text_length(label, fontname="helv", fontsize=fontsize)
            shape.insert_text(
                fitz.Point(cx - tw / 2, cy + fontsize * 0.36),
                label,
                fontsize=fontsize,
                fontname="helv",
                color=color,
            )

        shape.commit()

    return out


def characteristics_to_csv(characteristics: list[Characteristic]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "balloon", "page", "text", "category", "pattern", "family",
            "x", "y", "width", "height",
        ],
    )
    writer.writeheader()
    for c in characteristics:
        writer.writerow(asdict(c))
    return buf.getvalue().encode("utf-8-sig")


# =====================================================
# AUDIT MODE — decision trail, no balloons drawn
# =====================================================

_AUDIT_FIELDS = [
    "page",
    "block",
    "line",
    "raw_words",
    "merged_text",
    "x",
    "y",
    "w",
    "h",
    "pattern_matched",
    "category",
    "family",
    "accepted",
    "reason",
]


def _decide_audit_row(
    merged_text: str,
    x: float,
    y: float,
    page_rect: fitz.Rect,
) -> tuple[bool, str | None, str | None, str]:
    ok_id, id_kind, id_cat = _is_identity_combined(merged_text)
    if ok_id:
        return True, id_kind, id_cat, f"accepted: identity field {id_kind}"

    early = _early_reject_reason(merged_text)
    if early:
        return False, None, None, early

    is_dim, pattern_name, category = _is_dimension_verbose(merged_text)

    if not is_dim:
        if _looks_like_balloon_number(merged_text):
            return False, None, None, "rejected: existing item/balloon number"
        if _looks_like_id_code(merged_text):
            return False, None, None, "rejected: drawing/part ID code"
        if _looks_like_grid_number(merged_text, x, y, page_rect):
            return False, None, None, "rejected: grid/zone number band"
        return False, None, None, "rejected: no dimension pattern matched"

    if _in_title_block(x, y, page_rect):
        return False, pattern_name, category, "rejected: inside title block region"
    if _looks_like_grid_number(merged_text, x, y, page_rect):
        return False, pattern_name, category, "rejected: grid/zone number band"
    if _looks_like_id_code(merged_text):
        return False, pattern_name, category, "rejected: drawing/part ID code"
    if _looks_like_balloon_number(merged_text):
        return False, pattern_name, category, "rejected: existing item/balloon number"

    fam = _pattern_family(pattern_name)
    return True, pattern_name, category, f"accepted: matched {pattern_name} ({category}/{fam})"


def audit_page(page: fitz.Page, page_number: int = 1) -> list[dict]:
    """Log every dict-line through merge → classify → accept/reject."""
    rows: list[dict] = []
    raw_items = list(_iter_text_lines(page))
    base = [(t, r, s, d) for t, r, s, d, *_ in raw_items]
    merged = _merge_nearby_tol(_merge_stacked_glyphs(base))
    bound = _page_bound(page)

    for text, rect, size, direction in merged:
        accepted, pattern_name, category, reason = _decide_audit_row(
            text, float(rect.x0), float(rect.y0), bound
        )
        rows.append(
            {
                "page": page_number,
                "block": 0,
                "line": 0,
                "raw_words": text,
                "merged_text": text,
                "x": round(float(rect.x0), 1),
                "y": round(float(rect.y0), 1),
                "w": round(float(rect.width), 1),
                "h": round(float(rect.height), 1),
                "pattern_matched": pattern_name or "-",
                "category": category or "-",
                "family": _pattern_family(pattern_name) if accepted else "-",
                "accepted": accepted,
                "reason": reason,
            }
        )
    return rows


def audit_pdf(
    pdf_bytes: bytes,
    out_csv_path: str | None = None,
) -> tuple[list[dict], bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        rows: list[dict] = []
        for i in range(len(doc)):
            rows.extend(audit_page(doc[i], page_number=i + 1))
    finally:
        doc.close()

    rows.sort(key=lambda r: (not r["accepted"], r["reason"], r["page"], r["y"], r["x"]))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_AUDIT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buf.getvalue().encode("utf-8-sig")

    if out_csv_path:
        with open(out_csv_path, "wb") as f:
            f.write(csv_bytes)

    return rows, csv_bytes


def audit_reason_counts(rows: list[dict]) -> list[tuple[str, int]]:
    return Counter(r["reason"] for r in rows).most_common()


def audit_family_counts(rows: list[dict]) -> list[tuple[str, int]]:
    """Accepted spans broken down by indian vs generic pattern family."""
    return Counter(
        r.get("family", "-") for r in rows if r.get("accepted")
    ).most_common()


def audit_pattern_counts(rows: list[dict]) -> list[tuple[str, int]]:
    return Counter(
        r.get("pattern_matched", "-") for r in rows if r.get("accepted")
    ).most_common()
