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
import re
from collections import Counter
from dataclasses import asdict, dataclass

import fitz  # PyMuPDF

# =====================================================
# EXCLUSION REGISTRY — runs BEFORE pattern accept
# Indian OEM (IS/SP 46) + Tata-style title-block noise
# =====================================================

_EXCLUSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("skip_keyword", re.compile(
        r"(?i)^(sheet\s*(?:size|:|-)|sheet\s+\d+\s+of\b|rev(?:ision)?\b|date\b|scale\b|"
        r"dwg\b|drg\b|drawing\b|material\b|finish\b|note\b|"
        r"item\b|qty\b|part\b|page\b|proj\b|checked\b|approved\b|title\b|iso\b|asme\b|a[0-4]\b|"
        r"all\s+dims?\b|unless\b|otherwise\b|specified\b|detail\b|meters\b|approx\b|"
        r"millimeters\b|view\b|section\b|refer\b|general\b|welded\b|assembly\b|length\b).*"
    )),
    # Drafter / drawing stamps: DRD180828, DCC-TMP12-v2.0
    ("drafter_stamp", re.compile(r"(?i)^DRD\d{5,}$")),
    ("dcc_code", re.compile(r"(?i)^DCC[-A-Z0-9.]+$")),
    # IDs/part codes — not plate sizes, not metric threads, not DIA callouts
    ("id_code", re.compile(
        r"(?i)^(?!\d+[x×]\d+(?:[x×]\d+)?$)"
        r"(?!M\d)"
        r"(?!DIA)"
        r"(?=.*[A-Za-z])[A-Za-z0-9._\-]{5,}$"
    )),
    # IS 2102 general-tolerance note (title block)
    ("is_2102_note", re.compile(r"(?i).*AS\s+PER\s+IS\s*:?\s*2102.*")),
    ("is_note", re.compile(r"(?i).*AS\s+PER\s+IS\s*:?\s*\d+.*")),
    # IS material / spec citations: IS 513:2010 …
    ("is_material_spec", re.compile(r"(?i).*IS\s*\d+\s*:\s*\d{4}.*")),
    # ISO 2768 / IS roughness grade table words
    ("iso_2768_grade", re.compile(
        r"(?i).*\b(EXTRA\s+COARSE|COARSE|MEDIUM|FINE|VERY\s+FINE)\b.*"
    )),
    ("roughness_key", re.compile(
        r"(?i)^X?\s*0+(\s+0+)*(\s+X\s*0+)*\s*(EXTRA\s+COARSE|COARSE|MEDIUM|FINE)?$"
    )),
    ("thread_class_note", re.compile(r"(?i)^CLASS\s+\d+[HhGgEe]$")),
    # QTY / (x4) quantity markers — SP 46 practice
    ("qty_marker", re.compile(
        r"(?i)^\(?\s*[x×]\s*\d+\s*\)?$|"
        r"(?i)^QTY\b.*|"
        r"(?i)^\d+\s*NOS\.?$|"
        r"(?i)^NOS\.?$"
    )),
    # Note-reference numbers (not dimensions): SEE NOTE 3 / NOTE-3
    ("note_ref", re.compile(
        r"(?i)^(?:SEE\s+)?NOTES?\s*:?-?\s*\d+$|"
        r"(?i)^PRESS\s*FIT\b.*NOTE.*"
    )),
    # Length stated in meters only (mm closed/open lengths are real dims)
    ("meters_note", re.compile(
        r"(?i).*\d+(?:[.,]\d+)?\s*METERS?\b.*"
    )),
    ("grid_zone", re.compile(r"^[A-Z]$")),  # sheet letter columns only; not R10
    # Lone 1-digit counters / item markers — 2-digit mm dims are common on fab drawings
    ("bare_int", re.compile(r"^\d$")),
    ("long_int", re.compile(r"^\d{6,}$")),  # part numbers; 4–5 digit overall dims OK
    ("scale_ratio", re.compile(r"^\d+\s*:\s*\d+$")),
    ("date_like", re.compile(r"(?i).*\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}.*")),
    # Title-block Ra microfinish values (common false positives)
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
        r"(?i)^(?:DRAWING|DWG|DRG)\.?\s*NO\.?\s*:?$|"
        r"(?i)^(?:DRAWING|DWG|DRG)\.?\s*NUMBER\s*:?$"
    )),
    ("revision", re.compile(
        r"(?i)^REV(?:ISION)?\.?\s*:?$|"
        r"(?i)^REV\.?\s*FOLIO$"
    )),
    ("part_no", re.compile(
        r"(?i)^PART\s*(?:NO\.?|NUMBER|#)\s*:?$|"
        r"(?i)^P\s*/\s*N\s*:?$|"
        r"(?i)^P/?N$"
    )),
]

_IDENTITY_COMBINED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("drawing_no", re.compile(
        r"(?i)^(?:DRAWING|DWG|DRG)\.?\s*NO\.?\s*:?\s*"
        r"([A-Z]{0,4}\d{5,}(?:[-./][A-Z0-9]+)*)$"
    )),
    ("revision", re.compile(
        r"(?i)^REV(?:ISION)?\.?\s*:?\s*([A-Z0-9]{1,3})$"
    )),
    ("part_no", re.compile(
        r"(?i)^(?:PART\s*(?:NO\.?|NUMBER|#)|P\s*/\s*N|P/?N)\s*:?\s*"
        r"([A-Z]{0,4}\d{5,}(?:[-./][A-Z0-9]+)*)$"
    )),
]

_DRAWING_VALUE_RE = re.compile(
    r"(?i)^(?:DRD|DCC)?\d{6,}(?:[-./][A-Z0-9]+)*$|"
    r"(?i)^\d{6,}[-./][A-Z0-9]+(?:[-./][A-Z0-9]+)*$"
)
# Sheet rev: letter, letter+digit, or 01–99 (not bare 0)
_REV_VALUE_RE = re.compile(r"^(?:[A-Z]|[A-Z]\d|[1-9]\d?|0[1-9])$")
_PART_VALUE_RE = re.compile(
    r"(?i)^(?:DRD)?\d{6,}(?:[-./][A-Z0-9]+)*$"
)


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
        r"(?i)^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?"
        r"(?:\s*[HhGgFfEeNnPp]\d{1,2}(?:\s*/\s*[HhGgFfEeNnPp]\d{1,2})?)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL|THRU|THROUGH)(?:\s*HOLES?)?)?$"
    ), "diameter"),
    # O as ⌀ substitute — ONLY when adjacent to a decimal (avoids NOS/COARSE)
    ("diameter_O", re.compile(
        r"(?i)^O\s*\d+[.,]\d+(?:\s*(?:THRU|THROUGH))?(?:\s*\([^)]*\))?$"
    ), "diameter"),
    ("diameter_O_suffix", re.compile(
        r"(?i)^\d+[.,]\d+\s*O(?:\s*(?:THRU|THROUGH))?(?:\s*\([^)]*\))?$"
    ), "diameter"),
    ("diameter_dia", re.compile(
        r"(?i)^(?:DIA|DIAM)\.?\s*\d+(?:[.,]\d+)?"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?(?:\s*deep)?)?"
        r"(?:\s*holes?(?:-\d+\s*nos)?)?"
        r"(?:\s*\(\s*[x×]?\s*\d+\s*\))?$|"
        r"(?i)^\d+(?:[.,]\d+)?\s*(?:DIA|DIAM)\.?$"
    ), "diameter"),
    ("spherical_dia", re.compile(
        r"(?i)^S[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?$"
    ), "diameter"),
    # Multi-line hole callout kept as one token after merge
    ("diameter_thru_holes", re.compile(
        r"(?i)^[Ø⌀φΦ∅]?\s*\d+(?:[.,]\d+)?\s*\(\s*[x×]?\s*\d+\s*\)\s*"
        r"(?:THRU|THROUGH)\s*HOLES?$|"
        r"(?i)^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?\s*\(\s*[x×]?\s*\d+\s*\)\s*"
        r"(?:THRU|THROUGH)\s*HOLES?$"
    ), "diameter"),
    # OD / ID tube callouts (shop fab notes)
    ("od_id", re.compile(
        r"(?i)^(?:OD|ID)\s*\d+(?:[.,]\d+)?$|"
        r"(?i)^OD\s*\d+(?:[.,]\d+)?\s*[x×]\s*ID\s*\d+(?:[.,]\d+)?"
        r"(?:\s*-\s*\d+\s*long)?$"
    ), "diameter"),
    # Bush / pin OD notes on BOM: BUSH DIA-60- …
    ("bush_dia", re.compile(
        r"(?i)^BUSH\s*DIA\s*-?\s*\d+(?:[.,]\d+)?\b.*"
    ), "diameter"),
    # Ext / Int length callouts (European fab)
    ("ext_int", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*(?:Ext|Int|EXT|INT)\.?$"
    ), "linear"),
    # Chain dims wrapped in equals: =770±2=
    ("equals_tol", re.compile(
        r"^=\s*\d+(?:[.,]\d+)?\s*±\s*\d+(?:[.,]\d+)?\s*=$"
    ), "tolerance"),
    # Labeled geometric notes: Bore dia : 130 mm / Stroke : 1100 mm
    ("labeled_mm", re.compile(
        r"(?i)^(?:bore\s*dia|rod\s*dia|stroke|closed\s*length|open\s*length|"
        r"overall\s*length|width|height|depth|thickness|pitch|centres?)\s*:?\s*"
        r"\d+(?:[.,]\d+)?\s*mm$"
    ), "linear"),
    ("labeled_mm_generic", re.compile(
        r"(?i)^[A-Za-z][A-Za-z0-9\s/.\-]{0,28}:\s*\d+(?:[.,]\d+)?\s*mm$"
    ), "linear"),
    # Min / Max travel ranges on hydraulic sheets
    ("min_max_range", re.compile(
        r"(?i)^Min\s*\d+(?:[.,]\d+)?\s*(?:&|and)\s*Max\s*\d+(?:[.,]\d+)?$"
    ), "linear"),
    # Plate / section sizes — allow spaces: 1050 X 55, 776 X 25 X 25, 12x12
    ("plate_size", re.compile(
        r"(?i)^\d{2,}\s*[x×]\s*\d{2,}(?:\s*[x×]\s*\d{1,3})?"
        r"(?:\s*-\s*\d{2,5}\s*(?:long|nos))?$"
    ), "linear"),
    # Intermittent / stitch weld length pitch: 50x (20) x50
    ("weld_pitch", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*[x×]\s*"
        r"\(\s*\d+(?:[.,]\d+)?\s*\)\s*"
        r"[x×]\s*\d+(?:[.,]\d+)?$"
    ), "weld"),
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
        r"(?i)^BEND\s*R\s*\d+(?:[.,]\d+)?(?:\s*(?:TYP\.?|TYPICAL))?$|"
        r"(?i)^MIN\s*BEND\s*R(?:ADIUS)?\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*THK\.?)?$"
    ), "feature"),
    # Knurling / spotface compound notes
    ("knurl", re.compile(
        r"(?i)^KNURL\b.*\d+(?:[.,]\d+)?.*"
    ), "feature"),
    ("spotface", re.compile(
        r"(?i)^(?:SF|SPOT\s*FACE|C'?BORE|COUNTERBORE)\b.*\d+(?:[.,]\d+)?.*"
    ), "feature"),
    # Envelope requirement (ISO 8015 / IS 8000)
    ("envelope_e", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*Ⓔ\s*$|"
        r"(?i)^Ⓔ\s*\d+(?:[.,]\d+)?$"
    ), "tolerance"),
    # BOM material thickness notes (vertical BOM columns on Tata A2/A3)
    ("material_thk", re.compile(
        r"(?i)^(?:PLATE|L\s*ANGLES?|FLAT|SHEET|ERW\s*TUBE|BLOCK|TUBE)\s*-?\s*"
        r"\d+(?:[.,]\d+)?\s*THK\.?\b.*"
    ), "feature"),
    # Clearance hole class (IS/ISO fit on hole charts)
    ("clear_fit", re.compile(
        r"(?i)^CLEAR\s+[Hh]\d{1,2}$"
    ), "fit"),
    # BOM cut-length lines: a) 5500mm-4nos / a)-1095-6nos
    ("bom_cut_length", re.compile(
        r"(?i)^[a-z]\)\s*-?\d+(?:[.,]\d+)?(?:\s*mm)?"
        r"(?:\s*-\s*\d+\s*nos)?$"
    ), "linear"),
    # Clevis / plate BOM dims with qty suffix
    ("named_plate", re.compile(
        r"(?i)^(?:\d+\.\d+\s+)?clevis(?:\s+plate)?\s+"
        r"\d+[x×]\d+[x×]\d+(?:\s*-\s*\d+\s*nos)?$"
    ), "linear"),
    # Undercut / groove shop notes
    ("undercut", re.compile(
        r"(?i)^U/?C\s*-?\s*\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep$|"
        r"(?i)^UNDERCUT\s*\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?$|"
        r"(?i)^GROOVE\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)?$"
    ), "feature"),

    # --- BSP / pipe thread (IS 554) BEFORE radius — R 1/4 vs R 10.0 ---
    ("bsp_thread_R", re.compile(
        r"(?i)^R\s*\d+\s*/\s*\d+(?:\"|″)?(?:\s*(?:BSP|BSPT|BSPP))?$"
    ), "thread"),
    ("bsp_thread_G", re.compile(
        r"(?i)^(?:G\s*)?\d+(?:/\d+)?(?:\"|″)?\s*(?:BSP|BSPT|BSPP)"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep)?$|"
        r"(?i)^\d+\s*/\s*\d+(?:\"|″)?\s*(?:BSP|BSPT|BSPP)"
        r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?\s*deep)?$|"
        r"(?i)^G\s*\d+(?:/\d+)?$"
    ), "thread"),
    ("bsp_port_labeled", re.compile(
        r"(?i)^(?:pressure|breather)\s+port\s+size\s*:\s*"
        r"\d+(?:/\d+)?(?:\"|″)?\s*(?:BSP|BSPT|BSPP)$"
    ), "thread"),

    # --- radius (IS 10714) — decimal/integer after BSP fraction check ---
    ("radius", re.compile(
        r"(?i)^R\s*\d+(?:[.,]\d+)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$"
    ), "radius"),
    ("spherical_radius", re.compile(
        r"(?i)^SR\s*\d+(?:[.,]\d+)?(?:\s*\([^)]*\))?$"
    ), "radius"),

    # --- IS 919 / IS 2709 fit classes (ISO 286) ---
    # Omit M (metric thread) and S (SECTION marks on Tata sheets).
    ("fit_class", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*[ABCDEFGHJKNPRTUVYZ]\d{1,2}"
        r"(?:\s*/\s*[ABCDEFGHJKNPRTUVYZ]\d{1,2})?"
        r"(?:\s*\([+\-]?\d+(?:[.,]\d+)?(?:\s*/\s*[+\-]?\d+(?:[.,]\d+)?)?\))?$"
    ), "fit"),
    ("fit_nom_min_max", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*(?:NOM|MIN|MAX)$"
    ), "fit"),

    # --- threads IS 4218 metric ---
    ("metric_thread", re.compile(
        r"(?i)^M\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)?"
        r"(?:-\d+\s*nos)?"
        r"(?:\s*(?:THRU|THROUGH|TAP|TAPPING|DEEP|THRU\s+TAP(?:PING)?))?"
        r"(?:\s*\([^)]*\))?$"
    ), "thread"),
    ("unc_thread", re.compile(
        r"(?i)^(?:#\d+|\d+/\d+)\s*-\s*\d+\s*(?:UNC|UNF|UNEF|UN)(?:\s*-?\s*[123][AB])?$"
    ), "thread"),

    # --- angles / chamfers (IS convention — keep as one token) ---
    ("angle_deg", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*°(?:\s*\d{1,2}'(?:\s*\d{1,2}\")?)?"
        r"(?:\s*±\s*\d+(?:[.,]\d+)?°?)?"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$"
    ), "angle"),
    ("is_chamfer", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*°"
        r"(?:\s*(?:TYP\.?|TYPICAL))?(?:\s*\([^)]*\))?$|"
        r"(?i)^\d+(?:[.,]\d+)?\s*[x×]\s*45(?:\s*(?:TYP\.?|TYPICAL))?(?:\s*\([^)]*\))?$"
    ), "chamfer"),

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
        r"(?i)^\d+(?:[.,]\d+)?\s*THK\.?$"
    ), "feature"),
    # ISO thickness letter t (often vertical): t 10.0
    ("thickness_t", re.compile(
        r"(?i)^t\s*\d+(?:[.,]\d+)?$"
    ), "feature"),
    ("depth", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*DEEP$|"
        r"(?i)^[↧]\s*\d+(?:[.,]\d+)?$"
    ), "feature"),
    ("hole_thru", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*[Ø⌀φΦ∅O]?\s*(?:THRU|THROUGH)\s*(?:HOLES?)?"
        r"(?:\s*\([^)]*\))?$"
    ), "feature"),
    # Weld / feature size with TYP: 4 TYP, 3 TYP
    ("weld_typ", re.compile(
        r"(?i)^(?:[2-9]|1[0-2])\s*(?:TYP\.?|TYPICAL)$"
    ), "weld"),

    # --- reference / auxiliary ---
    ("reference", re.compile(
        r"(?i)^\(?\d+(?:[.,]\d+)?\)?\s*REF\.?(?:\s*\([^)]*\))?$"
    ), "reference"),
    ("ref_paren", re.compile(
        r"^\(\d+(?:[.,]\d+)?\)$"
    ), "reference"),

    ("linear_mm", re.compile(
        r"(?i)^\d+(?:[.,]\d+)?\s*mm$"
    ), "linear"),

    # Whole-mm integers + optional qty / TYP
    ("linear_integer", re.compile(
        r"(?i)^\d{2,5}"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$"
    ), "linear"),

    # Decimal linear + optional qty / TYP: 509.3 (x2), 20.0 TYP
    ("linear_decimal", re.compile(
        r"(?i)^\d+[.,]\d+"
        r"(?:\s*\(\s*[x×X]\s*\d+\s*\))?"
        r"(?:\s*(?:TYP\.?|TYPICAL))?$"
    ), "linear"),
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
        if re.match(r"(?i)^REV\.?\s*BY$", t):
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
    t = t.replace("\ufffd", "°")
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
            r"(?i)^(?!\d+[x×]\d+(?:[x×]\d+)?$)"
            r"(?!M\d)"
            r"(?!DIA)"
            r"(?=.*[A-Za-z])[A-Za-z0-9._\-]{5,}$",
            t,
        )
        or re.match(r"(?i)^DRD\d{5,}$", t)
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
    "fit_class",
    "clear_fit",
    "radius",
    # BOM material rows often sit beside notes headers on Tata sheets
    "plate_size",
    "material_thk",
    "bush_dia",
    "od_id",
    "labeled_mm",
    "thickness_t",
})


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
                min(bound.x1, label.x0 + w * 0.14),
                min(bound.y1, label.y1 + h * 0.22),
            )
        return fitz.Rect(
            max(bound.x0, label.x0 - 15),
            max(bound.y0, label.y0 - 8),
            min(bound.x1, label.x1 + w * 0.18),
            min(bound.y1, label.y0 + h * 0.22),
        )
    # title / identity block around label
    return fitz.Rect(
        max(bound.x0, label.x0 - 40),
        max(bound.y0, label.y0 - 40),
        min(bound.x1, label.x1 + w * 0.20),
        min(bound.y1, label.y1 + h * 0.18),
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

    # Classic title-block bands (right + bottom) — identity / tolerance tables
    title.append(fitz.Rect(
        bound.x0 + w * 0.78, bound.y0 + h * 0.55, bound.x1, bound.y1,
    ))
    title.append(fitz.Rect(
        bound.x0 + w * 0.55, bound.y0 + h * 0.88, bound.x1, bound.y1,
    ))
    # Top-right revision strip
    title.append(fitz.Rect(
        bound.x0 + w * 0.70, bound.y0, bound.x1, bound.y0 + h * 0.14,
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
    if zone == "drawing":
        return True
    if zone == "title":
        # Identity fields are added on a separate path; skip generic dims here
        return pattern in {"drawing_no", "revision", "part_no"}
    if zone == "bom":
        return pattern in _BOM_OK_PATTERNS
    if zone == "notes":
        return pattern in _NOTES_OK_PATTERNS
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
    if category == "fit" and re.match(r"(?i)^\d+(?:[.,]\d+)?\s*A[0-4]$", t):
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
            r"(?i)^(THK\.?|THRU|THROUGH|TAP|TAPPING|DEEP|NOM|MIN|MAX|REF\.?|"
            r"TYP\.?|TYPICAL|(?:THRU|THROUGH)\s*HOLES?|"
            r"[ABCDEFGHJKNPRTUVYZ]\d{1,2}|\(\s*[x×X]\s*\d+\s*\))$",
            _clean(t),
        ))

    def _is_limit_frag(t: str) -> bool:
        # Stacked fit limits drawn as separate lines: +0.030 / 0
        return bool(re.match(r"^[+\-±]?\s*\d+(?:[.,]\d+)?$", _clean(t))) and (
            t.strip().startswith(("+", "-", "±")) or _clean(t) in {"0", "0.0", "0.00"}
        )

    def _is_numeric_host(t: str) -> bool:
        t = _clean(t)
        return bool(
            re.match(r"^\(?\d+(?:[.,]\d+)?\)?$", t)
            or re.match(r"(?i)^M\d+(?:[.,]\d+)?$", t)
            or re.match(r"(?i)^R\s*\d+(?:[.,]\d+)?$", t)
            or re.match(r"(?i)^[Ø⌀φΦ∅]\s*\d+(?:[.,]\d+)?$", t)
            or re.match(r"(?i)^\d+(?:[.,]\d+)?\s*REF\.?$", t)
            or re.match(r"(?i)^\d+(?:[.,]\d+)?\s*[ABCDEFGHJKNPRTUVYZ]\d+$", t)
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
            lambda host: bool(re.match(r"(?i)^\d+(?:[.,]\d+)?\s*[A-Z]\d+$", host)),
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
    Circles already on the sheet (item balloons): near-round drawing bboxes
    in the typical balloon size range.
    Returns list of (cx, cy, radius).
    """
    circles: list[tuple[float, float, float]] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        # Item balloons vary by sheet scale — keep a wide but sane band
        if not (5.0 <= w <= 50.0 and 5.0 <= h <= 50.0):
            continue
        if max(w, h) / max(min(w, h), 0.1) > 1.55:
            continue
        items = d.get("items") or []
        ops = [it[0] for it in items]
        # Circles: bezier arcs and/or polygonal approximations
        if "c" not in ops and len(items) < 3:
            continue
        cx = (r.x0 + r.x1) / 2
        cy = (r.y0 + r.y1) / 2
        rad = max(w, h) / 2
        circles.append((cx, cy, rad))

    # Merge near-duplicates (double-stroked balloons)
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
    """
    True when a number already sits inside an item-balloon circle.
    Never re-balloon those — any numeric content.
    """
    if circles is None:
        circles = _existing_item_balloon_circles(drawings)

    # Fast path: centre of text inside / on a known round drawing
    for ocx, ocy, orad in circles:
        if math.hypot(cx - ocx, cy - ocy) <= orad * 1.2:
            return True

    # Round drawing envelope containing the glyph (segment-built circles)
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if not (5.0 <= w <= 50.0 and 5.0 <= h <= 50.0):
            continue
        if max(w, h) / max(min(w, h), 0.1) > 1.55:
            continue
        if not (r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1):
            continue
        rcx = (r.x0 + r.x1) / 2
        rcy = (r.y0 + r.y1) / 2
        rad = max(w, h) / 2
        # Number must be near the circle centre (not just grazing the bbox)
        if math.hypot(cx - rcx, cy - rcy) <= rad * 0.85:
            return True

    # Ring path around the glyph (CAD often strokes circles as segments)
    if size < 3.0:
        return False
    reach = max(size * 4.0, 22.0)
    pts = _collect_path_points(drawings, cx, cy, reach)
    if len(pts) < 8:
        return False

    for factor in (1.0, 1.2, 1.4, 1.65, 1.9, 2.15, 2.4, 2.7):
        rad = size * factor
        if not (4.5 <= rad <= 30.0):
            continue
        angs: list[float] = []
        tol = max(1.0, rad * 0.22)
        for px, py in pts:
            dist = math.hypot(px - cx, py - cy)
            if abs(dist - rad) <= tol:
                angs.append(math.atan2(py - cy, px - cx))
        if len(angs) < 5:
            continue
        bins = [0] * 12
        for a in angs:
            bins[int((a + math.pi) / (2 * math.pi) * 12) % 12] += 1
        if sum(1 for b in bins if b > 0) >= 6:
            return True
    return False


def _is_circled_number(
    text: str,
    cx: float,
    cy: float,
    size: float,
    drawings: list,
    circles: list[tuple[float, float, float]],
) -> bool:
    """
    Hard rule: any number already drawn inside a circle (item balloon)
    must never get an auto-balloon.
    """
    t = _clean(text)
    if not re.search(r"\d", t):
        return False
    if _is_inside_existing_balloon(cx, cy, size, drawings, circles):
        return True
    # Plain balloon indices 1–9999 sitting on/near a circle centre
    if re.match(r"^\d{1,4}$", t):
        for ocx, ocy, orad in circles:
            if math.hypot(cx - ocx, cy - ocy) <= orad * 1.55:
                return True
    return False


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


def _infer_balloon_circles_from_text(
    page: fitz.Page,
    drawings: list,
) -> list[tuple[float, float, float]]:
    """
    Item balloons whose stroke wasn't classified as a circle drawing:
    detect short integers wrapped by a near-circular path and treat them
    as obstacles / skip targets.
    """
    # Candidate roundish drawing envelopes (may be segment-built circles)
    round_boxes: list[tuple[float, float, float, float, float]] = []  # x0,y0,x1,y1,rad
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if not (5.0 <= w <= 50.0 and 5.0 <= h <= 50.0):
            continue
        if max(w, h) / max(min(w, h), 0.1) > 1.55:
            continue
        items = d.get("items") or []
        ops = [it[0] for it in items]
        if "c" not in ops and len(items) < 3:
            continue
        round_boxes.append((float(r.x0), float(r.y0), float(r.x1), float(r.y1), max(w, h) / 2))

    inferred: list[tuple[float, float, float]] = []
    for text, rect, size, *_rest in _iter_text_lines(page):
        t = _clean(text)
        if not re.match(r"^\d{1,3}$", t):
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        s = max(float(size), float(min(rect.width, rect.height)), 4.0)
        if s > 14.0:
            continue
        # Must sit inside a near-round drawing envelope
        host = None  # (hcx, hcy, rad)
        for x0, y0, x1, y1, rad in round_boxes:
            if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
                host = ((x0 + x1) / 2, (y0 + y1) / 2, rad)
                break
        if host is None:
            continue
        hcx, hcy, host_rad = host
        if math.hypot(cx - hcx, cy - hcy) <= host_rad * 1.1:
            inferred.append((hcx, hcy, host_rad * 1.08))

    out: list[tuple[float, float, float]] = []
    for cx, cy, rad in inferred:
        if any(math.hypot(cx - ox, cy - oy) < max(rad, orad) * 0.6 for ox, oy, orad in out):
            continue
        out.append((cx, cy, rad))
    return out


def _fillet_weld_anchors(page: fitz.Page) -> list[fitz.Point]:
    """
    Approximate fillet-weld symbol locations from vector drawings.
    Fillet marks are small right-triangles on the weld reference line
    (ISO 2553 / AWS A2.4). Size of the weld (3, 4, 6…) sits beside them.
    """
    anchors: list[fitz.Point] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return anchors

    for d in drawings:
        items = d.get("items") or []
        lines = [it for it in items if it[0] == "l"]
        if len(lines) < 2 or len(lines) > 8:
            continue
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        # Typical fillet triangle footprint on A2/A3/A0 sheets
        if not (2.5 <= w <= 20.0 and 2.5 <= h <= 22.0):
            continue
        ratio = max(w, h) / max(min(w, h), 0.1)
        if ratio > 3.5:
            continue
        filled = bool(d.get("fill"))
        # Filled fillets are common; stroked ones need a clear 3-edge outline
        if not filled and len(lines) not in (3, 4, 5, 6):
            continue
        # Tiny filled blobs without triangle proportions — skip
        if filled and max(w, h) < 3.5:
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
    Examples:
      6 △  (size left of fillet, above or below reference line)
      3 △  with weld-all-around circle on the leader
      4 △
    These are real inspection dims — do not miss them.
    """
    anchors = _fillet_weld_anchors(page)
    if not anchors:
        return []
    bound = _page_bound(page)
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        # Common metric fillet sizes (avoid 11/12 item-balloon false hits)
        if not re.match(r"^(?:[2-9]|10)$", t):
            continue
        if rect.x0 < bound.x0 + 55 or rect.x1 > bound.x1 - 55:
            continue
        if rect.y0 < bound.y0 + 40 or rect.y1 > bound.y1 - 40:
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        for a in anchors:
            dx = cx - a.x  # negative ⇒ size is LEFT of triangle (standard)
            dy = abs(cy - a.y)
            # Number sits left of △, on either side of the reference line
            if -55.0 <= dx <= -2.5 and dy <= max(size, 8.0) * 3.2:
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
        if len(lines) not in (2, 3):
            continue
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if not (2.0 <= w <= 12.0 and 2.0 <= h <= 14.0):
            continue
        anchors.append(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return anchors


def _surface_finish_candidates(
    page: fitz.Page,
    items: list[tuple[str, fitz.Rect, float, tuple]],
) -> list[tuple[str, fitz.Rect, float]]:
    """Lone roughness grade 1–4 next to a surface-texture V symbol."""
    anchors = _roughness_anchors(page)
    if not anchors:
        return []
    bound = _page_bound(page)
    out: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, _direction in items:
        t = _clean(text)
        if not re.match(r"^[1-4]$", t):
            continue
        if rect.x0 < bound.x0 + 40 or rect.x1 > bound.x1 - 40:
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        for a in anchors:
            if math.hypot(cx - a.x, cy - a.y) <= max(size, 6.0) * 2.5:
                out.append((t, rect, size))
                break
    return out


def _norm_dim_text(t: str) -> str:
    t = _clean(t).lower()
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
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        existing_circles = _existing_item_balloon_circles(drawings)
        existing_circles = existing_circles + _infer_balloon_circles_from_text(page, drawings)
        revision_tris = _existing_revision_triangles(drawings)
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
                from learning_store import match_learned as _match_learned
                _lab = _match_learned(accepted, page=page_index + 1, x=float(rect.x0), y=float(rect.y0))
                if _lab == "ignore":
                    continue
            except Exception:
                pass

            # Title-block / table filters — drawing zone, full-page only.
            # Tile recovery already excludes BOM/notes/title tile centers.
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
            found.append((page_index, accepted, rect, size, category, pattern))

        # Fillet weld sizes (3, 6, …) next to weld triangles — drawing / notes only
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
            # Weld size beside △ is OK; skip only if the digit itself is circled
            if _is_circled_number(text, tcx, tcy, size, drawings, existing_circles):
                continue
            if _is_revision_index_text(text) and _is_inside_revision_triangle(
                tcx, tcy, size, drawings, revision_tris
            ):
                continue
            if _is_same_dim(page_index + 1, text, rect, "weld_size", prior):
                continue
            found.append((page_index, text, rect, size, "weld", "weld_size"))

        # Surface-finish grades beside roughness V symbols
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

        # Drawing / revision / part numbers (title block — intentionally ballooned)
        # Skip when tiling a drawing clip (identity is page-level)
        if clip is None:
            for text, rect, size, pattern in _identity_field_candidates(items, bound):
                if not bound.intersects(rect):
                    continue
                tcx = (rect.x0 + rect.x1) / 2
                tcy = (rect.y0 + rect.y1) / 2
                if _is_inside_existing_balloon(tcx, tcy, max(size, 6.0), drawings, existing_circles):
                    continue
                if _is_same_dim(page_index + 1, text, rect, pattern, prior):
                    continue
                already_queued = False
                for pi, ft, fr, _, _, fp in found:
                    if pi != page_index:
                        continue
                    if fp == pattern and _norm_dim_text(ft) == _norm_dim_text(text):
                        already_queued = True
                        break
                    pcx = (fr.x0 + fr.x1) / 2
                    pcy = (fr.y0 + fr.y1) / 2
                    if math.hypot(pcx - tcx, pcy - tcy) < 14:
                        already_queued = True
                        break
                if already_queued:
                    continue
                found.append((page_index, text, rect, size, "identity", pattern))

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
    rows: int = 2,
    cols: int = 3,
) -> list[Characteristic]:
    """
    Recovery pass: scan drawing-zone tiles with wider merge so crowded
    geometry dims missed by the full-page pass can still be accepted.
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


def _renumber(chars: list[Characteristic]) -> list[Characteristic]:
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
            )
        )
    return out


def detect_dimensions_multipass(
    doc: fitz.Document,
    passes: int = 1,
    *,
    start_level: int = 1,
    already: list[Characteristic] | None = None,
    use_zones: bool = True,
    use_tiles: bool = True,
) -> tuple[list[Characteristic], list[dict]]:
    """
    Run detection up to `passes` times with rising recovery levels.
    Pass 1 = standard (zone-aware); later passes only add newly recovered dims.
    After the first pass, optionally run a drawing-tile recovery sweep.
    Stops early when a pass adds nothing (after the first pass).
    """
    passes = max(1, min(int(passes), 5))
    start_level = max(1, min(int(start_level), 5))
    accumulated = list(already or [])
    log: list[dict] = []

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
            tile_level = min(5, level + 1)
            tiled = detect_dimensions_tiled(
                doc, level=tile_level, already=accumulated, rows=3, cols=4,
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

        # After the opening pass, stop when recovery finds nothing new
        if i > 0 and len(added) == 0:
            break
        if already is not None and len(added) == 0 and i > 0:
            break

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
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        circles = _existing_item_balloon_circles(drawings)
        circles = circles + _infer_balloon_circles_from_text(page, drawings)
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


def apply_learned_dimensions(
    doc: fitz.Document,
    already: list[Characteristic],
) -> list[Characteristic]:
    """Accept spans the human marked as dimensions previously."""
    try:
        from learning_store import match_learned
    except ImportError:
        return []
    added: list[Characteristic] = []
    working = list(already)
    for page_index in range(len(doc)):
        page = doc[page_index]
        bound = _page_bound(page)
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        circles = _existing_item_balloon_circles(drawings)
        circles = circles + _infer_balloon_circles_from_text(page, drawings)
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
    from ai_fallback import classify_candidates
    from learning_store import add_feedback, few_shot_examples, match_learned

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
    passes: int = 1,
    *,
    use_zones: bool = True,
    use_tiles: bool = True,
    use_learning: bool = True,
    ai_fallback: bool = False,
    ai_provider: str = "openai",
    ai_api_key: str | None = None,
    ai_model: str | None = None,
) -> tuple[bytes, list[Characteristic], list[dict]]:
    """
    Balloon a drawing PDF.
    Order: vector multipass → learned human feedback → optional AI fallback.
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        chars, pass_log = detect_dimensions_multipass(
            src,
            passes=passes,
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
        ballooned = draw_balloons(src, _renumber(chars))
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
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    existing = _existing_item_balloon_circles(drawings)
    existing = existing + _infer_balloon_circles_from_text(page, drawings)
    revision_tris = _existing_revision_triangles(drawings)
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
    """
    out = fitz.open()
    out.insert_pdf(doc)

    by_page: dict[int, list[Characteristic]] = {}
    for c in characteristics:
        by_page.setdefault(c.page - 1, []).append(c)

    for page_i, items in by_page.items():
        page = out[page_i]
        shape = page.new_shape()

        for c, cx, cy, r in _spread_balloons(items, page):
            # Dimension target (leader tip)
            tx = c.x + c.width / 2.0
            ty = c.y + c.height / 2.0

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
            tip_p, left, right = _arrowhead(tip, start, size=max(3.2, r * 0.55))
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
