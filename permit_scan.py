import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import sqlite3
import threading
import time
import re
import json
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import shutil
import subprocess

_HOME          = os.path.expanduser("~")
# Defaults for a fresh install — the user's actual list lives in CONFIG_FILE
# ("scan_folders") and is editable from the Scan Watchers panel
SCAN_FOLDERS   = [r"U:\Documents\wscans", r"F:\scan"]
STAGING_FOLDER = os.path.join(_HOME, "Documents", "Permit Staging")
HISTORY_FILE   = os.path.join(_HOME, "permit_scan_history.json")
CONFIG_FILE    = os.path.join(_HOME, "permit_scan_config.json")
DEBUG_LOG      = os.path.join(_HOME, "permit_scan_debug.log")
DEBUG_LOG_MAX  = 2_000_000   # bytes — every log line is appended forever otherwise
SKIP_EXTENSIONS   = {".tmp", ".part", ".crdownload", ""}
TESSERACT_PATH    = os.path.join(_HOME, "AppData", "Local", "Programs", "Tesseract-OCR", "tesseract.exe")
STREET_LIST_FILE  = os.path.join(_HOME, "yorktown_streets.txt")

IGNORE_ADDRESSES = ["363 UNDERHILL AVE"]

# Source tracking
# tesseract_hw = Tesseract on handwriting — less reliable than Claude for numeric fields
# parcel       = county GIS parcel data (yorktown_parcels.db) — authoritative, outranks everything
# elec_cert / plan_review = fallback pages elsewhere in the batch — lowest rank,
#                fill-only (can never displace another source), county reconcile gatekeeps
_SOURCE_RANK = {"manual": 6, "parcel": 5, "native": 4, "tesseract": 3, "claude": 2,
                "tesseract_hw": 1, "elec_cert": 1, "plan_review": 1, "history": 1, "": 0}
_SRC_STYLE   = {
    "manual":       ("manual",     "#37474f"),
    "parcel":       ("county",     "#00695c"),
    "native":       ("text",       "#2e7d32"),
    "tesseract":    ("ocr",        "#e65100"),
    "claude":       ("ai  ←verify","#1565c0"),
    "tesseract_hw": ("ocr?",       "#92400e"),
    "elec_cert":    ("cert ←verify","#6a1b9a"),
    "plan_review":  ("plan ←verify","#6a1b9a"),
    "history":      ("hist ←verify","#00838f"),
}

def _load_known_streets():
    if not os.path.exists(STREET_LIST_FILE):
        return []
    with open(STREET_LIST_FILE) as f:
        return [ln.strip().upper() for ln in f if ln.strip()]

KNOWN_STREETS = _load_known_streets()


def fuzzy_match_street(street_name):
    """Snap an OCR/AI street name to the closest known Yorktown street.

    Cutoff 0.9, not 0.8 — measured against the real street list, 0.8 makes
    confident WRONG corrections: 'HEYWOOD ST' → 'WOOD ST' and 'HICKORY LN' →
    'HICKORY ST'. Spelled-out suffixes ('SABER COURT') are already handled by
    normalize_suffix before they reach here, so the tighter cutoff costs nothing.
    NOTE: the blank-gate at the end of _extract_fields drops any machine-read
    street this function can't place — raising the cutoff makes that gate fire
    more often (blank rather than wrong, which is the intended trade).
    """
    import difflib
    if not KNOWN_STREETS or not street_name:
        return street_name
    hits = difflib.get_close_matches(street_name.upper(), KNOWN_STREETS, n=1, cutoff=0.9)
    return hits[0] if hits else street_name

STREET_SUFFIXES = (
    r"STREET|AVENUE|BOULEVARD|PARKWAY|HIGHWAY|TERRACE|CIRCLE|COURT|DRIVE|PLACE|ROAD|LANE|"
    r"BLVD|PKWY|HWY|TERR|TRL|ALY|"
    r"ST|AVE|RD|DR|CT|PL|CIR|TER|LN|LA|WAY|LOOP|RUN|TR"
)

SITE_LABELS = [
    r"address[/\s]+location\s+of\s+(?:property|work)",
    r"location\s+of\s+work",
    r"job\s+(?:site\s+)?address",
    r"site\s+address",
    r"property\s+address",
    r"premises",
    r"job\s+location",
    r"work\s+(?:site\s+)?address",
    r"address\s+of\s+(?:work|job|premises)",
    r"location\s+of\s+(?:construction|building|work|project)",
    # "present address of owner" / "address of owner" intentionally excluded —
    # those are the owner's mailing address, not the job site.
    r"location",
]


# ── OCR / extraction helpers ───────────────────────────────────────────────────

_OFFICIAL_PERMIT_TYPES = re.compile(
    r'(?:BUILDING|DEMOLITION|ELECTRICAL|PLUMBING|MECHANICAL|POOL|FENCE|SIGN|FIRE)\s+PERMIT',
    re.IGNORECASE,
)

# INSPECTION PROCEDURE info sheets carry "Building Permit" + the Yorktown
# letterhead, so they false-positive as official — which ends the page sweep
# before a real permit deeper in the batch is reached. (Also the known
# oversized-but-not-a-plan form in merge categorization.)
_INSPECTION_SHEET_RE = re.compile(r'INSPECTION\s+PROCEDURE', re.IGNORECASE)

def detect_permit_type(text):
    upper = text.upper()
    # Application check FIRST — application forms contain "BUILDING PERMIT" + Yorktown markers
    # which would otherwise match the official check below. Covers all permit types:
    # "Application for a Demolition Permit" was slipping through as "official",
    # which let Tesseract's read of its handwritten fields outrank Claude's.
    # \s* tolerates OCR-merged spacing: quick-pass Tesseract reads the stylized
    # header as "APPLICATION FORA DEMOLITION PERMIT", which \s+ missed
    if re.search(r'APPLICATION\s+FOR\s*A?\s*'
                 r'(?:BUILDING|DEMOLITION|ELECTRICAL|PLUMBING|MECHANICAL|POOL|FENCE|SIGN|FIRE)'
                 r'\s+PERMIT', upper):
        return "application"
    # Structural fallback when the header itself is unreadable: only application
    # forms carry "(Office use only)" boxes next to an Application No. label
    if re.search(r'OFFICE\s+USE\s+ONLY', upper) and re.search(r'APPLICATION\s+(?:NO|#|FEE)', upper):
        return "application"
    if _INSPECTION_SHEET_RE.search(upper):
        return "unknown"
    if _OFFICIAL_PERMIT_TYPES.search(upper) and (
        re.search(r'TOWN\s+OF\s+YORKTOWN', upper) or
        re.search(r'BUILDING\s+DEPARTMENT', upper) or
        '363 UNDERHILL' in upper
    ):
        return "official"
    return "unknown"



def extract_text_from_page(doc, page_idx, printed=False):
    """Tesseract OCR a single already-open fitz page; combines native + OCR text.
    PyMuPDF applies PDF page rotation automatically when rendering — do NOT re-rotate.
    printed=True: the page is a known printed form — grayscale PSM 3/6 only,
    since contrast enhancement and PSM 11 help handwriting but degrade clean print."""
    import fitz
    import pytesseract
    from PIL import Image
    import io
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    _t0 = time.time()
    page = doc[page_idx]
    native = page.get_text().strip()
    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
    png_bytes = pix.tobytes("png")

    # Pass A: grayscale only — best for clean printed scans
    img_gray = Image.open(io.BytesIO(png_bytes)).convert("L")
    if printed:
        ocr = ocr_image(img_gray, pytesseract,
                        configs=["--oem 1 --psm 3", "--oem 1 --psm 6"])
        metric("ocr_invoked", duration_ms=int((time.time() - _t0) * 1000), detail="printed")
        return (native + "\n" + ocr).strip()
    ocr_a = ocr_image(img_gray, pytesseract)

    # Pass B: contrast-enhanced — better for faint handwritten text
    img_proc = preprocess_for_ocr(Image.open(io.BytesIO(png_bytes)))
    ocr_b = ocr_image(img_proc, pytesseract)

    ocr = ocr_a if _ascii_score(ocr_a) >= _ascii_score(ocr_b) else ocr_b
    metric("ocr_invoked", duration_ms=int((time.time() - _t0) * 1000), detail="dual")
    return (native + "\n" + ocr).strip()


def quick_page_text(doc, page_idx):
    """Cheap text for one page: native layer if substantial, else single-pass
    1.5× Tesseract. A few stray native characters (scanner artifacts) are not
    enough to classify on — those pages still get OCR'd."""
    import fitz
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    native = doc[page_idx].get_text().strip()
    if len(native) > 40:
        return native
    pix  = doc[page_idx].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    gray = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")
    try:
        ocr = pytesseract.image_to_string(gray, config="--psm 3 --oem 3")
    except Exception:
        ocr = ""
    return (native + "\n" + ocr).strip()


def quick_ocr_page_type(doc, page_idx):
    """Single-pass 1.5× Tesseract classification of one page — detection only."""
    return detect_permit_type(quick_page_text(doc, page_idx))


# ── Merge-on-confirm ordering ──────────────────────────────────────────────────
# All staged files are assembled into ONE PDF at Confirm & Rename, in filing
# order: official permit first, everything else in scan order, building plans
# near the end, orange folder cover last.
_IMAGE_EXTS  = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp')
_STAGE_EXTS  = ('.pdf',) + _IMAGE_EXTS
_MERGE_ORDER = {"official": 0, "other": 1, "plans": 2, "orange": 3}
# Name a file gets at Confirm & Rename: "{8 digits}{optional suffix}.pdf" (the
# " OPEN|CLOSED" middle is legacy — filings before 2026-07-29 carry it and must
# still be recognized), plus the " - 2" form used when an earlier filing of the
# same permit is still in staging. Raw scanner output ("20260722153247958.pdf")
# never matches (digits past the first 8). Used to tell already-confirmed files
# apart from a new batch after an app restart.
_FINAL_NAME_RE = re.compile(r'^\d{8}[A-Za-z]*(?:\s+(?:OPEN|CLOSED))?(?:\s+-\s+\d+)?\.pdf$',
                            re.IGNORECASE)
_LEGAL_AREA  = 612 * 1008   # 8.5×14 in PDF points — anything well past this is plan-sized
# Orange folder cover labels (preprinted, so Tesseract reads them reliably)
_ORANGE_LABEL_RE  = re.compile(r'BLDG\.?\s*PER|LOCATION\s+OF\s+PROJECT', re.IGNORECASE)


def _plan_sized(page):
    """Plan-sheet-sized page — never a permit form, and slow to OCR."""
    return page.rect.width * page.rect.height > _LEGAL_AREA * 1.4


def _orange_cover_page(page):
    """Orange-folder cover detected by paper color. Covers laid flat on the
    11x17 plan scanner come out plan-sized, so the OCR sweeps skip them and
    the label regex never runs — but the orange paper itself is unmistakable.
    Renders a thumbnail and counts orange-hued pixels."""
    import fitz
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(0.15, 0.15))
    except Exception:
        return False
    if pix.n < 3:
        return False
    s, n = pix.samples, pix.n
    total = pix.width * pix.height
    if not total:
        return False
    hits = 0
    for i in range(0, total * n, n):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if r > 140 and r - b > 50 and b + 15 < g < r:
            hits += 1
    return hits / total > 0.35


# ── Fallback data sources (added 2026-07-22) ──────────────────────────────────
# Census over 1,222 scanned batches showed two printed page types that reliably
# carry parcel info when the permit/orange cover fail: electrical inspection
# certificates ("Located at {site}", Section/Block/Lot — SBL agreed with the
# official permit 90 times in validation, and the disagreements were OCR garbage
# the county reconcile rejects) and plan-review list pages ("... CONSTRUCTION
# PROPOSED AT {address}", typed). Both rank below every other source and only
# fill still-empty fields. A cert's own application/certificate number mimics a
# permit ID and was one-digit-off wrong 3/23 times — permit from a cert is
# logged as a suggestion, never filled.
_ELEC_CERT_RE = re.compile(
    r'BOARD\s+OF\s+FIRE\s+UNDERWRITERS|BUREAU\s+OF\s+ELECTRICITY|ELECTRICAL\s+INSPECTION',
    re.IGNORECASE)
# Plan-review letters phrase it many ways ("the work proposed at X", "the
# construction proposed at X", "plan review list for the ... proposed at X");
# the stable anchor is "proposed at {number street}". OCR sometimes splits
# number and street across a comma/newline ("395,\nSaber Court").
_PLAN_REVIEW_ADDR_RE = re.compile(
    r'PROPOSED\s+AT[:\s]*(\d+\s*,?\s*[A-Za-z][^\n]{2,60})',
    re.IGNORECASE)


def _trim_site_address(addr):
    """Cut a captured address down to 'number street': drop town/state/zip tail,
    stop at the street suffix."""
    addr = re.sub(r'\s+', ' ', addr)                       # collapse newlines from wrapped lines
    addr = re.sub(r'^(\d+)\s*,\s*', r'\1 ', addr)          # OCR comma after the house number
    addr = re.sub(r',?\s*(Yorktown|New York|NY|\d{5}).*$', '', addr, flags=re.IGNORECASE).strip()
    m = re.search(rf'\b({STREET_SUFFIXES})\b\.?', addr, re.IGNORECASE)
    if m:
        addr = addr[:m.end()].strip().rstrip(',.')
    return addr.strip()


def _sweep_fallback_pages(doc, exclude_idx):
    """Quick-scan the file's other pages for electrical certs / plan-review
    lists. Returns a dict with any of: "address" -> (value, source, page_1based),
    "sbl" -> (value, "elec_cert", page), "permit_hint" -> (value, page)."""
    out = {}
    for i in range(len(doc)):
        if i == exclude_idx or _plan_sized(doc[i]):
            continue
        text = quick_page_text(doc, i)
        if _ELEC_CERT_RE.search(text):
            if "address" not in out:
                m = re.search(r'LOCATED\s+AT[:\s]+(\d[^\n]{3,70})', text, re.IGNORECASE)
                if m:
                    val = _trim_site_address(m.group(1))
                    if val:
                        out["address"] = (val, "elec_cert", i + 1)
            if "sbl" not in out:
                v = find_sbl(text)
                if v:
                    out["sbl"] = (v, "elec_cert", i + 1)
            if "permit_hint" not in out:
                v = find_permit_number(text)
                if v:
                    out["permit_hint"] = (v, i + 1)
        elif "address" not in out:
            m = _PLAN_REVIEW_ADDR_RE.search(text)
            if m:
                val = _trim_site_address(m.group(1))
                if val:
                    out["address"] = (val, "plan_review", i + 1)
        if "address" in out and "sbl" in out and "permit_hint" in out:
            break
    return out


def _claude_page_png(page, zoom):
    """Render a page for the Claude API, keeping the PNG under the 5 MB image
    limit — a plan-sized orange folder at 3× is ~9 MB and gets rejected with a
    400. Long side capped at ~2400 px (the API downscales past ~1600 px anyway,
    so oversized scans lose nothing), then steps down if the PNG is still big."""
    import fitz
    long_pts = max(page.rect.width, page.rect.height) or 1
    z = min(zoom, 2400 / long_pts)
    while True:
        png = page.get_pixmap(matrix=fitz.Matrix(z, z)).tobytes("png")
        if len(png) <= 4_500_000 or z <= 0.5:
            return png
        z *= 0.8


def try_flip_official(doc, page_idx, classify, log=None):
    """Flip a scanned page 180° and re-classify. The official permit is always
    the earliest sheet of a batch and is sometimes fed upside-down — never
    sideways — so an unreadable early page must be flip-checked BEFORE trusting
    any page after it. If the flip reveals the official permit: keep it,
    incremental-save, and sweep the rest of the doc so the Laserfiche copy is
    fully upright. Otherwise the rotation is restored. Returns True on reveal."""
    orig = doc[page_idx].rotation
    doc[page_idx].set_rotation((orig + 180) % 360)
    if classify(doc, page_idx) == "official":
        if log:
            log(f"[..] Page {page_idx + 1} was scanned upside-down — flipped it")
        try:
            doc.saveIncr()
        except Exception as e:
            if log:
                log(f"[!]  Could not save flip fix to PDF: {e}")
        fix_upside_down_pages(doc, log=log)
        return True
    doc[page_idx].set_rotation(orig)
    return False


def fix_upside_down_pages(doc, pages=None, log=None):
    """Detect pages scanned in the wrong orientation (Tesseract OSD) and repair
    the PDF rotation flag in place. Returns the list of fixed page indexes.
    Renders honor the corrected flag immediately; the incremental save makes it
    permanent so the copy filed into Laserfiche is upright too.
    Scanners may already stamp a rotation flag, so OSD's correction is added to
    the existing value rather than replacing it."""
    import fitz
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    fixed = []
    for i in (pages if pages is not None else range(len(doc))):
        page = doc[i]
        if page.get_text().strip():   # digital text layer — orientation is fine
            continue
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")
        try:
            osd  = pytesseract.image_to_osd(img)
            rot  = int(re.search(r'Rotate:\s*(\d+)', osd).group(1))
            conf = float(re.search(r'Orientation confidence:\s*([\d.]+)', osd).group(1))
        except Exception:
            continue   # blank or handwritten page — OSD can't tell, leave it alone
        if rot and conf >= 5.0:
            page.set_rotation((page.rotation + rot) % 360)
            fixed.append(i)
    if fixed:
        try:
            doc.saveIncr()
        except Exception as e:
            if log:
                log(f"[!]  Could not save rotation fix to PDF: {e}")
    return fixed


def preprocess_for_ocr(img):
    from PIL import ImageEnhance, ImageFilter
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _ascii_score(t):
    return sum(1 for c in t if c.isascii() and (c.isalnum() or c in ' \n.,:-#/'))


def ocr_image(img, pytesseract, configs=None):
    if configs is None:
        configs = [
            "--oem 1 --psm 3",   # LSTM + auto layout detection (good for forms)
            "--oem 1 --psm 6",   # LSTM + uniform block
            "--oem 1 --psm 11",  # LSTM + sparse/handwritten layout
        ]
    results = [pytesseract.image_to_string(img, config=c) for c in configs]
    return max(results, key=_ascii_score)



def find_sbl(text):
    # Format 1: already assembled — e.g. 48.11-1-11 or 36.5-2-27
    # [.,]+ handles OCR artifact "16,.10" (comma+period) from scanned permits
    m = re.search(r'\b(\d{1,3}[.,]+\d{1,3}\s*-\s*\d{1,3}\s*-\s*\d{1,3})\b', text)
    if m:
        val = m.group(1).replace(' ', '')
        val = re.sub(r',+', '', val)      # remove stray commas
        val = re.sub(r'\.{2,}', '.', val) # collapse double-periods
        return val

    # Format 2: SECTION ___ BLOCK ___ LOT(S) ___ on a handwritten form.
    # Find each field independently. Allow "-" as decimal (OCR misreads "." as "-").
    # Use loose boundary before block/lot since OCR often merges adjacent chars.
    sec_m = re.search(r'\b(?:section|sec)\.?\s*[^\d\n]{0,20}(\d{1,3}(?:[.,\- ]\d{1,3})?)', text, re.IGNORECASE)
    blk_m = re.search(r'\b(?:block|blk)\.?\s*[^\d\n]{0,20}(\d{1,3})',                      text, re.IGNORECASE)
    lot_m = re.search(r'\blot[^\d\n]{0,20}(\d{1,3})',                                       text, re.IGNORECASE)

    if sec_m and blk_m and lot_m:
        section = re.sub(r'[\-,\s]', '.', sec_m.group(1).strip())
        return f"{section}-{blk_m.group(1)}-{lot_m.group(1)}"

    return ""


def find_application_number(text):
    """Return the raw Application No. value (e.g. '2010-0656'), or empty string."""
    m = re.search(r'application\s*(?:no?|number|#|num)[^\d\n]{0,15}([\d][\d\-]{3,10}[\d])', text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


_PERMIT_ID_SUFFIXES = re.compile(r'\s*(DEMO|FD|RES|COM|ALT|ADD|NEW|POOL|ELEC|PLMB|MECH)\b', re.IGNORECASE)
# Suffix glued directly to the digits ("20160001FD") is part of the permit ID even
# when it's not a known type — but only if ALL CAPS, so a following word with a
# dropped space ("20160001File") can't be swallowed.
_PERMIT_ID_GLUED_SUFFIX = re.compile(r'([A-Z]{1,6})(?![a-zA-Z])')

def find_permit_number(text, blocked_digits=None):
    """Return permit ID (8 digits + optional suffix like DEMO), skipping candidates matching blocked_digits."""
    patterns = [
        r'permit\s*(?:no?|number|#|num)[^\d\n]{0,15}(\d[\d /\-]{0,12}\d)',
        r'permit\s*(?:no?|number|#|num)[^\d\n]{0,15}(\d+)',
        # Orange folder label: "BLDG. PER No. __________"
        r'bldg\.?\s*per(?:mit)?\.?\s*(?:no?|number|#|num)?\.?[^\d\n]{0,10}(\d[\d /\-]{0,12}\d)',
        r'bldg\.?\s*per(?:mit)?\.?\s*(?:no?|number|#|num)?\.?[^\d\n]{0,10}(\d+)',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw = m.group(1)
            if '-' in raw:
                continue
            digits = re.sub(r'\D', '', raw)
            if len(digits) == 8:
                if blocked_digits and digits == blocked_digits:
                    continue
                # Check for a permit-type suffix immediately after the matched digits.
                # Known suffixes may be spaced ("20100027 DEMO"); unknown ones count
                # only when glued to the digits ("20160001FD") — a spaced word there
                # is just the next label ("20160001 File Date").
                tail = text[m.end():m.end() + 8]
                sm = _PERMIT_ID_SUFFIXES.match(tail)
                suffix = sm.group(1).upper() if sm else ""
                if not suffix:
                    gm = _PERMIT_ID_GLUED_SUFFIX.match(tail)
                    if gm:
                        suffix = gm.group(1)
                return digits + suffix
    return ""


def find_address(text):
    # Inspection sheet format: "Job: Surname, 459 Crow Hill Road, description"
    # The address starts after the first comma (past the owner name).
    m = re.search(r'\bjob\s*[:\-_]+\s*[^,\n]+,\s*((?:\d+[\w\-]*\s+)[^\n\r]{3,60})', text, re.IGNORECASE)
    if m:
        addr = re.sub(r'\s+', ' ', m.group(1)).strip().upper()
        suffix_m = re.search(rf'\b({STREET_SUFFIXES})\b', addr)
        if suffix_m:
            end = suffix_m.end()
            if end < len(addr) and addr[end] == '.':
                end += 1
            addr = addr[:end].strip().rstrip(',.')
            addr = re.sub(r',?\s*(YORKTOWN|NEW YORK|NY|\d{5}).*$', '', addr, flags=re.IGNORECASE).strip()
            if addr and re.match(r'^\d', addr) and not any(ig in addr for ig in IGNORE_ADDRESSES):
                return addr

    for label in SITE_LABELS:
        pattern = rf'(?:{label})[\s:.\-]*((?:\d+[\w\-]*\s+)[^\n\r]{{3,60}})'
        for m in re.finditer(pattern, text, re.IGNORECASE):
            addr = re.sub(r'\s+', ' ', m.group(1)).strip().upper()
            suffix_m = re.search(rf'\b({STREET_SUFFIXES})\b', addr)
            if suffix_m:
                end = suffix_m.end()
                if end < len(addr) and addr[end] == '.':
                    end += 1
                addr = addr[:end].strip().rstrip(',.')
            addr = re.sub(r',?\s*(YORKTOWN|NEW YORK|NY|\d{5}).*$', '', addr, flags=re.IGNORECASE).strip()
            if not addr or not re.match(r'^\d', addr):
                continue
            if not re.match(r'^[A-Z0-9\s\.\-]+$', addr, re.IGNORECASE):
                continue
            if any(ig in addr for ig in IGNORE_ADDRESSES):
                continue
            return addr
    return ""


SUFFIX_ABBR = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
    "CIRCLE": "CIR", "COURT": "CT", "DRIVE": "DR",
    "PLACE": "PL", "ROAD": "RD", "LANE": "LN",
    "TRAIL": "TRL", "ALLEY": "ALY", "LOOP": "LOOP",
}

def normalize_suffix(name):
    for full, abbr in SUFFIX_ABBR.items():
        name = re.sub(rf'\b{full}\.?$', abbr, name.strip(), flags=re.IGNORECASE)
    return name.strip()


def split_address(full_address):
    m = re.match(r'^(\d+(?:-\d+)?)\s+(.+)$', full_address.strip())
    if m:
        return m.group(1), normalize_suffix(m.group(2))
    return "", normalize_suffix(full_address.strip())


# ── County parcel data (yorktown_parcels.db, built by build_parcel_db.py) ─────
# Address ↔ SBL for every Yorktown parcel from the NYS GIS assessment roll.
# Lets us fill either field from the other and catch OCR misreads against
# authoritative data instead of trying to read the form harder.

PARCEL_DB_FILE = os.path.join(_HOME, "yorktown_parcels.db")


def parcel_db_available():
    return os.path.exists(PARCEL_DB_FILE)


def _parcel_query(sql, params=()):
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{PARCEL_DB_FILE}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def _norm_street_key(street):
    return re.sub(r'\s+', ' ', street.upper().rstrip('.').strip())


def _sbl_candidates(sbl):
    """Plausible official print_key forms of an as-read SBL, best guess first.

    Official sections are always N.NN / NN.NN (fraction exactly 2 digits), so:
    - '5910-1-4'  → try '59.10-1-4'   (OCR dropped the decimal point)
    - '16.6-1-3'  → try '16.06-1-3' and '16.60-1-3'  (handwriting drops the zero)
    Leading zeros on integer parts are stripped ('01' → '1').
    """
    s = re.sub(r'\s+', '', sbl or '')
    parts = s.split('-')
    if len(parts) < 3:
        return [s] if s else []

    def clean(p):
        m = re.match(r'^0*(\d+)(\.\d+)?$', p)
        return (m.group(1) + (m.group(2) or '')) if m else p

    parts = [clean(p) for p in parts]
    sec, rest = parts[0], parts[1:]
    secs = [sec]
    if '.' in sec:
        whole, frac = sec.split('.', 1)
        if len(frac) == 1:
            secs += [f"{whole}.0{frac}", f"{whole}.{frac}0"]
    elif sec.isdigit() and 3 <= len(sec) <= 4:
        secs.append(f"{int(sec[:-2])}.{sec[-2:]}")
    out = []
    for sc in secs:
        cand = '-'.join([sc] + rest)
        if cand not in out:
            out.append(cand)
    return out


def parcel_lookup_sbl(sbl):
    """Match an as-read SBL against county data. Returns (print_key, addr) or None.

    Condo complexes: permits carry the base lot (15.20-1-28) but the county roll
    only lists the subdivided unit keys (15.20-1-28.1-1, ...). A candidate whose
    units exist is accepted as-is, with no address (units span many numbers)."""
    for cand in _sbl_candidates(sbl):
        rows = _parcel_query("SELECT print_key, addr FROM parcels WHERE print_key=?", (cand,))
        if rows:
            return rows[0]
    for cand in _sbl_candidates(sbl):
        rows = _parcel_query("SELECT COUNT(*) FROM parcels WHERE print_key LIKE ?",
                             (cand + '.%',))
        if rows and rows[0][0]:
            return (cand, None)
    return None


def _county_resolves(sbl):
    """Does this as-read SBL resolve to a real county parcel, allowing the same
    OCR damage reconcile can repair (dropped decimal/zero, lost leading digit)?
    Returns (print_key, addr) or None."""
    if not sbl or not parcel_db_available():
        return None
    hit = parcel_lookup_sbl(sbl)
    if hit:
        return hit
    tail = re.sub(r'\s+', '', sbl)
    near = _parcel_query("SELECT print_key, addr FROM parcels WHERE print_key LIKE ?",
                         ('%' + tail,)) if tail else []
    return near[0] if len(near) == 1 else None


def parcel_lookup_address(num, street):
    """Find the parcel for a street number + name. Returns (print_key, street) or None.

    Falls back to suffix-agnostic matching (HICKORY LN → HICKORY ST) when the
    exact street has no such number — but only if the match is unambiguous."""
    if not num or not street:
        return None
    st = _norm_street_key(street)
    rows = _parcel_query("SELECT print_key, street FROM parcels WHERE st_nbr=? AND street=?",
                         (str(num), st))
    if rows:
        return rows[0]
    base = re.sub(rf'\s+({STREET_SUFFIXES})\.?$', '', st, flags=re.IGNORECASE).strip()
    if base and base != st:
        rows = _parcel_query(
            "SELECT print_key, street FROM parcels WHERE st_nbr=? AND (street=? OR street LIKE ?)",
            (str(num), base, base + ' %'))
        if len({r[1] for r in rows}) == 1:
            return rows[0]
    return None


# Abbreviated street suffixes get a period in Laserfiche ("CROW HILL RD.",
# "MOHANSIC AVE. EAST"); spelled-out endings ("OLD COUNTRY WAY") do not.
# Never dot the first word — "LA VOIE CT" starts with LA as a real word.
_LF_ABBREV_SUFFIXES = {"RD", "ST", "AVE", "DR", "LA", "LN", "CT", "PL", "CR",
                       "BLVD", "TERR", "TER", "PKWY", "TR", "EST", "CIR", "HWY", "EXT"}


def laserfiche_path_for(num, street):
    """Laserfiche folder path for an address (module-level so lookups can use it)."""
    num = str(num or "").strip()
    words = [w.rstrip('.') for w in (street or "").strip().upper().split()]
    if not words:
        return ""
    st_p = " ".join(w + "." if i and w in _LF_ABBREV_SUFFIXES else w
                    for i, w in enumerate(words))
    # No street number: the parcel folder is named after the street itself
    # ("...\D\DARBY ST.\DARBY ST.").
    leaf = f"{num} {st_p}" if num else st_p
    return rf"TownOfYorktown\Building Department\Parcels\{st_p[0]}\{st_p}\{leaf}"


def parcel_search(query, limit=60):
    """Free-form parcel finder for the Property window. Accepts an SBL (or SBL
    prefix like '16.17'), an address ('3269 Stony St'), a bare street number,
    a street name, or an owner name. Returns (print_key, addr, owner) rows."""
    q = (query or "").strip().upper()
    if not q:
        return []
    # SBL-shaped: digits with a dash or decimal ('16.17-2-75', '16.17')
    if re.match(r'^[\d.\-\s]+$', q) and ('-' in q or '.' in q):
        for cand in (_sbl_candidates(q) or [q.replace(' ', '')]):
            rows = _parcel_query(
                "SELECT print_key, addr, owner FROM parcels "
                "WHERE print_key=? OR print_key LIKE ? "
                "ORDER BY print_key LIMIT ?",
                (cand, cand + '-%', limit))
            if rows:
                return rows
        return []
    # Bare street number
    if q.isdigit():
        return _parcel_query(
            "SELECT print_key, addr, owner FROM parcels WHERE st_nbr=? ORDER BY street LIMIT ?",
            (q, limit))
    # Number + street
    m = re.match(r'^(\d+)\s+(.+)$', q)
    if m:
        rows = _parcel_query(
            "SELECT print_key, addr, owner FROM parcels WHERE st_nbr=? AND street LIKE ? "
            "ORDER BY street LIMIT ?",
            (m.group(1), _norm_street_key(m.group(2)) + '%', limit))
        if rows:
            return rows
    # Street name
    rows = _parcel_query(
        "SELECT print_key, addr, owner FROM parcels WHERE street LIKE ? AND addr != '' "
        "ORDER BY street, CAST(st_nbr AS INTEGER) LIMIT ?",
        (_norm_street_key(q) + '%', limit))
    if rows:
        return rows
    # Owner name
    return _parcel_query(
        "SELECT print_key, addr, owner FROM parcels WHERE owner LIKE ? "
        "ORDER BY owner LIMIT ?",
        ('%' + q + '%', limit))


def archive_scans_for_parcel(print_key, addr):
    """Every archived scan for a parcel, matched by SBL (normalized or not) or
    by address with punctuation stripped. Same row shape as archive_search."""
    con = _archive_con()
    try:
        sbls = {print_key, re.sub(r'\.0(\d)', r'.\1', print_key)}  # 36.05-… also as 36.5-…
        pat = None
        if addr:
            pat = re.sub(r'\s+', ' ', addr.upper().replace('.', '')).strip() + '%'
        return con.execute(
            f"""SELECT id, scanned_at, permit_id, address, sbl, status, final_name, orig_name,
                       substr(text, 1, 200)
                FROM scans
                WHERE sbl IN ({','.join('?' * len(sbls))})
                   OR (? IS NOT NULL AND REPLACE(REPLACE(UPPER(address), '.', ''), ',', '') LIKE ?)
                ORDER BY id DESC""",
            (*sbls, pat, pat)).fetchall()
    finally:
        con.close()


def reconcile_with_parcels(num, street, sbl, sources, log):
    """Cross-check extracted fields against county parcel data.

    - Repairs OCR-mangled SBLs (dropped decimal, dropped zero-padding) when the
      repaired form exists in the county data.
    - Corrects a wrong street suffix when number + base name is unambiguous.
    - Fills a missing SBL from the address (and vice versa) — source "parcel".
    - Flags an SBL that contradicts the address WITHOUT overwriting a plausible
      one — permits are historical and parcels do get renumbered.
    Returns possibly-updated (num, street, sbl, sources).
    """
    if not parcel_db_available():
        log("[--] County check skipped — parcel database not found")
        return num, street, sbl, sources

    # Every reconcile run must say SOMETHING — the silent paths made it look
    # like the county check wasn't running at all.
    said = []
    def say(msg):
        said.append(msg)
        log(msg)

    addr_hit = parcel_lookup_address(num, street) if (num and street) else None
    if addr_hit and _norm_street_key(street) != addr_hit[1]:
        say(f"[..] Street corrected from county data: '{street}' → '{addr_hit[1]}'")
        street = addr_hit[1]

    sbl_hit = parcel_lookup_sbl(sbl) if sbl else None
    if sbl and sbl_hit and sbl_hit[0] != re.sub(r'\s+', '', sbl):
        say(f"[..] SBL repaired from county data: '{sbl}' → '{sbl_hit[0]}'")
        sbl = sbl_hit[0]

    # Address didn't resolve to any parcel (garbled/truncated street) but the SBL
    # did — if the county's street number for that parcel matches the number we
    # read, trust the county's street name. Fixes cut-off reads like '3269 STO'.
    if sbl_hit and not addr_hit and num and street and sbl_hit[1]:
        m = re.match(r'^(\d+)\s+(.+)$', sbl_hit[1])
        if m and m.group(1) == str(num):
            say(f"[..] Street repaired from county data (via SBL): '{street}' → '{m.group(2)}'")
            street = m.group(2)
            sources["address"] = "parcel"
            addr_hit = (sbl_hit[0], m.group(2))
        elif m:
            say(f"[!]  Address '{num} {street}' not in county data; SBL {sbl_hit[0]} "
                f"belongs to {sbl_hit[1]} — verify")

    if sbl and not sbl_hit:
        if addr_hit:
            say(f"[!]  SBL '{sbl}' not in county data — using {addr_hit[0]} (from address) instead")
            sbl = addr_hit[0]
            sources["sbl"] = "parcel"
        else:
            # Handwriting often loses the LEADING digit ('6.11-3-3' for
            # '16.11-3-3') — a unique suffix match in county data recovers it.
            tail = re.sub(r'\s+', '', sbl)
            near = _parcel_query(
                "SELECT print_key, addr FROM parcels WHERE print_key LIKE ?",
                ('%' + tail,)) if tail else []
            # A unique suffix match alone is a guess — a MISREAD SBL can suffix-
            # match some unrelated parcel. Only repair when that parcel's county
            # address agrees with the address read off the form; otherwise log
            # the candidate as a suggestion and leave the field alone.
            _corr = False
            if len(near) == 1 and near[0][1]:
                m = re.match(r'^(\d+)\s+(.+)$', near[0][1])
                if m:
                    _corr = (num and m.group(1) == str(num)) or \
                            (street and (m.group(2) == _norm_street_key(street) or
                                         fuzzy_match_street(street) == m.group(2)))
            if len(near) == 1 and _corr:
                say(f"[..] SBL repaired from county data: '{sbl}' → '{near[0][0]}' "
                    f"(leading digit lost in scan; address matches) — verify")
                sbl = near[0][0]
                sources["sbl"] = "parcel"
                sbl_hit = (near[0][0], near[0][1])
            elif len(near) == 1:
                say(f"[!]  SBL '{sbl}' not in county data — closest parcel is "
                    f"{near[0][0]} ({near[0][1] or 'no address'}), but nothing read off the "
                    "form confirms it — verify")
            elif 1 < len(near) <= 4:
                opts = "; ".join(f"{k} ({a})" for k, a in near)
                say(f"[!]  SBL '{sbl}' not in county data — near matches: {opts} — verify")
            else:
                say(f"[!]  SBL '{sbl}' not found in county parcel data — verify")
    elif sbl and sbl_hit and addr_hit and sbl_hit[0] != addr_hit[0]:
        say(f"[!]  SBL/address conflict: form says {sbl_hit[0]}, county lists "
            f"{num} {street} as {addr_hit[0]} — verify before filing")

    # A street name that isn't any real Yorktown street can't be right — if the
    # (validated) SBL knows the parcel's address, county wins over the garble.
    if sbl_hit and sbl_hit[1] and street and not addr_hit \
            and fuzzy_match_street(street) == street and street.upper() not in KNOWN_STREETS:
        m = re.match(r'^(\d+)\s+(.+)$', sbl_hit[1])
        if m:
            say(f"[..] Address replaced from county data: '{street}' is not a Yorktown "
                f"street — county lists {sbl_hit[0]} as {sbl_hit[1]} — verify")
            num, street = m.group(1), m.group(2)
            sources["address"] = "parcel"
            addr_hit = (sbl_hit[0], m.group(2))

    if not sbl and addr_hit:
        sbl = addr_hit[0]
        sources["sbl"] = "parcel"
        say(f"[OK] SBL filled from county parcel data: {sbl}")

    if not street and sbl_hit and sbl_hit[1]:
        m = re.match(r'^(\d+)\s+(.+)$', sbl_hit[1])
        if m:
            num, street = m.group(1), m.group(2)
            sources["address"] = "parcel"
            say(f"[OK] Address filled from county parcel data: {num} {street}")

    # Make a clean verification visible — silent success looks like nothing ran
    if sbl and addr_hit and sbl == addr_hit[0]:
        sources["verified"] = True
        say(f"[OK] Verified against county parcels: {num} {street} = {sbl}")

    # Cover the formerly-silent paths: reconcile always reports its outcome
    if not said:
        if not sbl and not (num and street):
            say("[--] County check: nothing extracted to check against")
        elif sbl_hit and not addr_hit:
            say(f"[--] County check: SBL {sbl_hit[0]} is a real parcel; "
                "address could not be cross-checked — verify address")
        elif (num and street) and not sbl:
            say(f"[--] County check: '{num} {street}' has no exact county match "
                "and no SBL to cross-check — verify")
        else:
            say("[--] County check ran — nothing conclusive")

    return num, street, sbl, sources


# ── Claude vision helpers ──────────────────────────────────────────────────────

def load_claude_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                key = json.load(f).get("anthropic_api_key", "")
        except Exception:
            pass
    return key

def save_claude_key(key):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            pass
    cfg["anthropic_api_key"] = key
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def load_scan_folders():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                folders = json.load(f).get("scan_folders")
            if isinstance(folders, list) and folders:
                return [os.path.normpath(fo) for fo in folders]
        except Exception:
            pass
    return list(SCAN_FOLDERS)

def save_scan_folders(folders):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            pass
    cfg["scan_folders"] = list(folders)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

_WINDOW_POS_RE = re.compile(r'^[+-]\d+[+-]\d+$')

def load_window_pos():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                pos = json.load(f).get("window_pos", "")
            if _WINDOW_POS_RE.match(pos):
                return pos
        except Exception:
            pass
    return ""

def save_window_pos(pos):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            pass
    cfg["window_pos"] = pos
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def extract_fields_with_claude(page_png_bytes, api_key, app_no="", model="claude-haiku-4-5-20251001"):
    import anthropic, base64
    # Same page image + model + context always yields the same answer — serve
    # repeats from the cache instead of paying the API again
    cache_key = _claude_cache_key(page_png_bytes, model, app_no)
    cached = _claude_cache_get(cache_key)
    if cached is not None:
        metric("vision_cache_hit", model=model)
        return cached
    client = anthropic.Anthropic(api_key=api_key)
    img_b64 = base64.standard_b64encode(page_png_bytes).decode()
    app_no_hint = (
        f"The Application No. on this form is '{app_no}' — do NOT return this value or its digits "
        f"('{re.sub(chr(45), '', app_no)}') as permit_id under any circumstances. "
    ) if app_no else (
        "The form also has an 'Application No.' field (usually above Permit No., formatted with a hyphen "
        "like '2010-0656') — do NOT return that value under any circumstances. "
    )
    prompt = (
        "This is a building permit form from the Town of Yorktown. "
        "Forms may be fully printed, fully handwritten, or mixed. Extract ONLY these fields:\n"
        "- permit_id: ONLY the value from the field labeled 'Permit No.', 'Permit #', 'Building Permit No.', "
        "or 'BLDG. PER No.' (orange folder cover sheets). "
        "On Application for Building Permit forms, 'PERMIT No.' appears directly BELOW 'APPLICATION No.' — "
        "read ONLY the 'PERMIT No.' line; ignore 'APPLICATION No.' entirely. "
        + app_no_hint +
        "If the Permit No. field is blank or absent, return empty string. "
        "Permit IDs are 8 digits, sometimes followed by a type suffix with no space (e.g. '20100027DEMO'). "
        "Return the digits and suffix together, no hyphens — e.g. '20100240' or '20100027DEMO'\n"
        "- address: the job site address — where the construction work is being performed. "
        "On printed Building Permits this is labeled 'Location:'. "
        "On Application for Building Permit forms, the label is 'ADDRESS/LOCATION OF PROPERTY' — "
        "that is the correct field. Do NOT return the value from 'Present Address of Owner', "
        "which appears above it on the same form and is the owner's home address (may differ from the job site). "
        "On orange folder cover sheets the label is 'LOCATION OF PROJECT'. "
        "IMPORTANT: the form header always starts with 'Town of Yorktown / 363 Underhill Avenue / Yorktown Heights' — "
        "that is the Building Department's address, NOT the job site. Never return '363 Underhill Avenue'. "
        "If the address field says 'as above' or is blank, return empty string.\n"
        "- section: number from the SECTION or SBL SECTION field\n"
        "- block: number from the BLOCK field\n"
        "- lot: number from the LOT or LOT(S) field\n\n"
        "Return ONLY a JSON object, nothing else. Use empty string for anything unclear.\n"
        'Example: {"permit_id":"20100240","address":"1005 East Main St","section":"16.10","block":"4","lot":"25"}'
    )
    # Structured outputs force the reply to match this schema — on pages that
    # don't look like the forms described above, the model otherwise narrates
    # in prose instead of returning JSON. (Assistant prefill is not supported
    # on Sonnet 4.6.)
    schema = {
        "type": "object",
        "properties": {
            "permit_id": {"type": "string"},
            "address":   {"type": "string"},
            "section":   {"type": "string"},
            "block":     {"type": "string"},
            "lot":       {"type": "string"},
        },
        "required": ["permit_id", "address", "section", "block", "lot"],
        "additionalProperties": False,
    }
    _t0 = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=300,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    _usage = getattr(response, "usage", None)
    _tin = getattr(_usage, "input_tokens", None)
    _tout = getattr(_usage, "output_tokens", None)
    metric("vision_invoked", model=model,
           duration_ms=int((time.time() - _t0) * 1000),
           tokens_in=_tin, tokens_out=_tout,
           cost_usd=_claude_cost(model, _tin, _tout))
    raw = response.content[0].text.strip() if response.content else ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{[^{}]*\}', raw)   # last-resort: fish the object out of prose
        if not m:
            raise
        data = json.loads(m.group(0))
    permit_id = str(data.get("permit_id", "")).strip()
    address   = str(data.get("address",   "")).strip()
    section   = str(data.get("section",   "")).strip()
    block     = str(data.get("block",     "")).strip()
    lot       = str(data.get("lot",       "")).strip()
    sbl = f"{section}-{block}-{lot}" if (section and block and lot) else ""
    _claude_cache_put(cache_key, permit_id, address, sbl, model)
    return permit_id, address, sbl


# ── History helpers ────────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []


def append_history(permit_id, address, sbl, status=""):
    # status is legacy (OPEN/CLOSED dropped from the workflow 2026-07-29) —
    # the key stays so old rows and new rows share a shape
    history = load_history()
    history.insert(0, {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "permit_id": permit_id,
        "address": address,
        "sbl": sbl,
        "status": status,
    })
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[:1000], f, indent=2)


# ── Full-text scan archive ─────────────────────────────────────────────────────
# Every scan's OCR/native text is kept in permit_scan_archive.db and indexed
# with FTS5 — a searchable full-text record of every document ever scanned.
# Rows are created at extraction time and stamped with the confirmed permit
# metadata + final filename + Laserfiche path at Confirm & Rename.

ARCHIVE_DB_FILE = os.path.join(_HOME, "permit_scan_archive.db")


def _archive_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _archive_con():
    con = sqlite3.connect(ARCHIVE_DB_FILE)
    con.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY,
        scanned_at   TEXT,
        confirmed_at TEXT,
        orig_name    TEXT,
        final_name   TEXT,
        permit_id    TEXT DEFAULT '',
        address      TEXT DEFAULT '',
        sbl          TEXT DEFAULT '',
        status       TEXT DEFAULT '',
        form_type    TEXT DEFAULT '',
        lf_path      TEXT DEFAULT '',
        text         TEXT DEFAULT '')""")
    try:
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS scans_fts USING fts5(
            permit_id, address, sbl, text, content='scans', content_rowid='id')""")
        con.executescript("""
            CREATE TRIGGER IF NOT EXISTS scans_ai AFTER INSERT ON scans BEGIN
                INSERT INTO scans_fts(rowid, permit_id, address, sbl, text)
                VALUES (new.id, new.permit_id, new.address, new.sbl, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS scans_ad AFTER DELETE ON scans BEGIN
                INSERT INTO scans_fts(scans_fts, rowid, permit_id, address, sbl, text)
                VALUES ('delete', old.id, old.permit_id, old.address, old.sbl, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS scans_au AFTER UPDATE ON scans BEGIN
                INSERT INTO scans_fts(scans_fts, rowid, permit_id, address, sbl, text)
                VALUES ('delete', old.id, old.permit_id, old.address, old.sbl, old.text);
                INSERT INTO scans_fts(rowid, permit_id, address, sbl, text)
                VALUES (new.id, new.permit_id, new.address, new.sbl, new.text);
            END;""")
    except sqlite3.OperationalError:
        pass  # FTS5 unavailable — archive_search falls back to LIKE
    # Claude answers are deterministic per page image — cache them so repeat
    # runs (watcher + Re-OCR + rescans of the same sheet) never pay twice
    con.execute("""CREATE TABLE IF NOT EXISTS claude_cache (
        key        TEXT PRIMARY KEY,
        permit_id  TEXT DEFAULT '',
        address    TEXT DEFAULT '',
        sbl        TEXT DEFAULT '',
        model      TEXT DEFAULT '',
        created_at TEXT)""")
    return con


def _claude_cache_key(page_png_bytes, model, app_no):
    import hashlib
    h = hashlib.sha256(page_png_bytes)
    h.update(f"|{model}|{app_no}".encode())
    return h.hexdigest()


def _claude_cache_get(key):
    con = _archive_con()
    try:
        row = con.execute("SELECT permit_id, address, sbl FROM claude_cache WHERE key=?",
                          (key,)).fetchone()
        return tuple(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _claude_cache_put(key, permit_id, address, sbl, model):
    con = _archive_con()
    try:
        con.execute("INSERT OR REPLACE INTO claude_cache VALUES (?,?,?,?,?,?)",
                    (key, permit_id, address, sbl, model, _archive_now()))
        con.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()


def archive_record_scan(path, text, form_type):
    """Insert or refresh the raw-text row for a scanned file (pre-confirmation)."""
    con = _archive_con()
    try:
        name = os.path.basename(path)
        row = con.execute("SELECT id FROM scans WHERE orig_name=? AND confirmed_at IS NULL",
                          (name,)).fetchone()
        if row:
            con.execute("UPDATE scans SET text=?, form_type=?, scanned_at=? WHERE id=?",
                        (text, form_type, _archive_now(), row[0]))
        else:
            con.execute("INSERT INTO scans (scanned_at, orig_name, form_type, text) VALUES (?,?,?,?)",
                        (_archive_now(), name, form_type, text))
        con.commit()
    finally:
        con.close()


def archive_confirm(renamed_pairs, permit, address, sbl, status, lf_path):
    """Stamp this batch's rows with confirmed metadata. renamed_pairs: [(orig_name, final_name)]."""
    con = _archive_con()
    try:
        now = _archive_now()
        for orig, final in renamed_pairs:
            row = con.execute("SELECT id FROM scans WHERE orig_name=? AND confirmed_at IS NULL",
                              (orig,)).fetchone()
            if row:
                con.execute("""UPDATE scans SET confirmed_at=?, final_name=?, permit_id=?,
                               address=?, sbl=?, status=?, lf_path=? WHERE id=?""",
                            (now, final, permit, address, sbl, status, lf_path, row[0]))
            else:
                # File staged without extraction (e.g. extra pages skipped by early exit)
                con.execute("""INSERT INTO scans (scanned_at, confirmed_at, orig_name, final_name,
                               permit_id, address, sbl, status, lf_path)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (now, now, orig, final, permit, address, sbl, status, lf_path))
        con.commit()
    finally:
        con.close()


def history_lookup_permit(permit_id):
    """Permit → property memory: distinct parcels this permit was confirmed-filed
    under. Returns [(address, sbl, last_confirmed_at, final_name)], newest first.
    Exactly one row = safe to auto-fill (with verify flag); more = ambiguous, abstain."""
    con = _archive_con()
    try:
        return con.execute("""
            SELECT address, sbl, MAX(confirmed_at), final_name FROM scans
            WHERE permit_id=? AND confirmed_at IS NOT NULL AND ifnull(sbl,'')<>''
            GROUP BY sbl ORDER BY 3 DESC""", (permit_id,)).fetchall()
    finally:
        con.close()


def history_prior_filings(permit_id):
    """Confirmed filings of this permit: [(final_name, confirmed_at, status, lf_path)],
    newest first. Used by the duplicate check — a hit may be a legitimate REVISED rescan."""
    con = _archive_con()
    try:
        return con.execute("""
            SELECT final_name, MAX(confirmed_at), status, lf_path FROM scans
            WHERE permit_id=? AND confirmed_at IS NOT NULL AND ifnull(final_name,'')<>''
            GROUP BY final_name ORDER BY 2 DESC""", (permit_id,)).fetchall()
    finally:
        con.close()


def archive_search(query, limit=200):
    """Search the archive. FTS5 (prefix-matching the last word) with LIKE fallback.
    Returns rows: (id, scanned_at, permit_id, address, sbl, status, final_name, orig_name, snippet)."""
    con = _archive_con()
    try:
        q = (query or "").strip()
        base_cols = "id, scanned_at, permit_id, address, sbl, status, final_name, orig_name"
        if not q:
            return con.execute(f"""SELECT {base_cols}, substr(text, 1, 200) FROM scans
                                   ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        toks = [t.replace('"', '') for t in q.split() if t.replace('"', '')]
        if not toks:
            return []
        fts_q = ' '.join(f'"{t}"' for t in toks[:-1]) + f' "{toks[-1]}"*'
        try:
            return con.execute("""
                SELECT s.id, s.scanned_at, s.permit_id, s.address, s.sbl, s.status,
                       s.final_name, s.orig_name,
                       snippet(scans_fts, 3, '»', '«', ' … ', 14)
                FROM scans_fts JOIN scans s ON s.id = scans_fts.rowid
                WHERE scans_fts MATCH ? ORDER BY rank LIMIT ?""",
                (fts_q.strip(), limit)).fetchall()
        except sqlite3.OperationalError:
            like = f"%{q}%"
            return con.execute(f"""SELECT {base_cols}, substr(text, 1, 200) FROM scans
                                   WHERE text LIKE ? OR permit_id LIKE ? OR address LIKE ? OR sbl LIKE ?
                                   ORDER BY id DESC LIMIT ?""",
                               (like, like, like, like, limit)).fetchall()
    finally:
        con.close()


def archive_get(row_id):
    """Full record for the detail view. Returns a dict or None."""
    con = _archive_con()
    try:
        row = con.execute("""SELECT scanned_at, confirmed_at, orig_name, final_name, permit_id,
                             address, sbl, status, form_type, lf_path, text
                             FROM scans WHERE id=?""", (row_id,)).fetchone()
        if not row:
            return None
        keys = ("scanned_at", "confirmed_at", "orig_name", "final_name", "permit_id",
                "address", "sbl", "status", "form_type", "lf_path", "text")
        return dict(zip(keys, row))
    finally:
        con.close()


def archive_count():
    con = _archive_con()
    try:
        return con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    finally:
        con.close()


def archive_backfill_from_history():
    """One-time import of pre-archive history entries (metadata only, no text)."""
    con = _archive_con()
    try:
        if con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]:
            return 0
        n = 0
        for e in load_history():
            con.execute("""INSERT INTO scans (scanned_at, confirmed_at, permit_id, address, sbl, status)
                           VALUES (?,?,?,?,?,?)""",
                        (e.get("date", ""), e.get("date", ""), e.get("permit_id", ""),
                         e.get("address", ""), e.get("sbl", ""), e.get("status", "")))
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def rotate_debug_log():
    """Cap the debug log at one rollover generation. Called once at startup —
    every _log line appends to this file, so over months it grows unbounded."""
    try:
        if os.path.exists(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > DEBUG_LOG_MAX:
            os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
    except Exception:
        pass


# ── File watcher ───────────────────────────────────────────────────────────────

# ── Operational telemetry ──────────────────────────────────────────────────────
# Sanitized by design: events carry event names, document classes, sources,
# timings, statuses, and token counts — NEVER document text, addresses, permit
# numbers, or owner names. That makes the whole metrics DB safe to export for
# dashboards without a scrubbing step. Keep it that way: no new field may carry
# content read off a scanned document.

METRICS_DB_FILE = os.path.join(_HOME, "permit_scan_metrics.db")

# USD per million tokens (input, output) — estimated-cost accounting only.
# Anthropic list prices as of 2026-08 (Haiku 4.5: $1/$5, Sonnet 4.6: $3/$15).
_MODEL_PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}

# One batch = one permit's worth of scans, ended by New Permit. Module-level so
# module functions (Claude call, OCR) can attribute events without plumbing.
_ACTIVE_BATCH = {"id": ""}


def _new_batch_id():
    _ACTIVE_BATCH["id"] = "b" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    return _ACTIVE_BATCH["id"]


def _claude_cost(model, tokens_in, tokens_out):
    rate = _MODEL_PRICES.get(model)
    if not rate or tokens_in is None or tokens_out is None:
        return None
    return (tokens_in * rate[0] + tokens_out * rate[1]) / 1_000_000


def metric(event, doc_class="", source="", field="", status="", model="",
           duration_ms=None, tokens_in=None, tokens_out=None, cost_usd=None,
           detail=""):
    """Fire-and-forget operational telemetry. Swallows every failure —
    telemetry must never take down or slow the scanning workflow."""
    try:
        con = sqlite3.connect(METRICS_DB_FILE, timeout=2)
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                ts          TEXT NOT NULL,
                event       TEXT NOT NULL,
                batch_id    TEXT DEFAULT '',
                doc_class   TEXT DEFAULT '',
                source      TEXT DEFAULT '',
                field       TEXT DEFAULT '',
                status      TEXT DEFAULT '',
                model       TEXT DEFAULT '',
                duration_ms INTEGER,
                tokens_in   INTEGER,
                tokens_out  INTEGER,
                cost_usd    REAL,
                detail      TEXT DEFAULT '')""")
            con.execute(
                "INSERT INTO events (ts, event, batch_id, doc_class, source, field,"
                " status, model, duration_ms, tokens_in, tokens_out, cost_usd, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), event,
                 _ACTIVE_BATCH["id"], doc_class, source, field, status, model,
                 duration_ms, tokens_in, tokens_out, cost_usd, detail))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


class ScanHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=self._wait_and_notify, args=(event.src_path,), daemon=True).start()

    def _wait_and_notify(self, path):
        # Large plan TIFs on the network share can pause mid-write for over a
        # second, so one stable 0.5s sample isn't proof the scanner is done —
        # suspected cause of two plan sheets vanishing from a merge (2026-07-27).
        # Require the size to hold for 3 consecutive checks, and wait up to
        # 2 minutes for the biggest sheets.
        prev = -1
        stable = 0
        for _ in range(240):
            try:
                size = os.path.getsize(path)
            except OSError:
                time.sleep(0.5)
                continue
            if size == prev and size > 0:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev = size
            time.sleep(0.5)
        self.callback(path)


# ── Main app ───────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Permit Scan Assistant")
        self.resizable(False, False)

        self.permit_id  = tk.StringVar()
        self.street_num  = tk.StringVar()
        self.street_name = tk.StringVar()
        self.sbl         = tk.StringVar()
        self.last_ocr_text = ""
        self._prog_set = False   # True while code (not the user) writes the field vars

        # Each entry: {"current": path_in_staging, "renamed": False}
        self.staged = []
        self.file_class = {}   # path → classified form type, cached for merge ordering
        self.batch_verified = False   # county-verified triple on the form — skip Claude for later files
        self._scanning = False
        self._src_labels = {}
        self._current_sources = {"permit": "", "address": "", "sbl": ""}

        # Watched folders (from config) + per-folder active toggles; vars are
        # created lazily in _folder_var so rebuilds don't stack duplicate traces
        self.scan_folders = load_scan_folders()
        self.folder_active = {}

        os.makedirs(STAGING_FOLDER, exist_ok=True)
        rotate_debug_log()
        _new_batch_id()

        self._build_ui()
        self._restore_window_pos()
        self._start_watchers()
        self._recover_staging()

        for v in (self.permit_id, self.street_num, self.street_name):
            v.trace_add("write", lambda *_: self._refresh_path())
        self.sbl.trace_add("write", lambda *_: self._validate_sbl())
        # A write that isn't guarded by _prog_set came from the user typing —
        # relabel the field "manual" so a stale "ai ←verify" doesn't outlive the fix
        for v, field in ((self.permit_id, "permit"), (self.street_num, "address"),
                         (self.street_name, "address"), (self.sbl, "sbl")):
            v.trace_add("write", lambda *_, f=field: self._field_edited(f))
        self.bind("<Key>", self._on_hotkey)
        self.after(100, self._update_stats)
        self.after(150, self._init_archive)

    def _init_archive(self):
        try:
            imported = archive_backfill_from_history()
            if imported:
                self._log(f"[--] Archive created — imported {imported} history record(s)")
            self._log(f"[--] Archive: {archive_count()} scan(s) indexed")
        except Exception as e:
            self._log(f"[!]  Archive init failed: {e}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        p = {"padx": 12, "pady": 6}

        info = ttk.LabelFrame(self, text="Permit Info", padding=10)
        info.grid(row=0, column=0, **p, sticky="ew")

        ttk.Label(info, text="Permit ID").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.permit_id, width=30).grid(row=0, column=1, padx=8, pady=2)
        self._src_labels["permit"] = tk.Label(info, text="", font=("Consolas", 8), width=12, anchor="w", relief="flat", bd=0)
        self._src_labels["permit"].grid(row=0, column=2, sticky="w")

        ttk.Label(info, text="Street Number").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.street_num, width=10).grid(row=1, column=1, padx=8, pady=2, sticky="w")

        ttk.Label(info, text="Street Name").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.street_name, width=30).grid(row=2, column=1, padx=8, pady=2)
        self._src_labels["address"] = tk.Label(info, text="", font=("Consolas", 8), width=12, anchor="w", relief="flat", bd=0)
        self._src_labels["address"].grid(row=2, column=2, sticky="w")

        ttk.Label(info, text="SBL").grid(row=3, column=0, sticky="w", pady=2)
        sbl_frame = ttk.Frame(info)
        sbl_frame.grid(row=3, column=1, padx=8, pady=2, sticky="w")
        self._sbl_entry = ttk.Entry(sbl_frame, textvariable=self.sbl, width=18)
        self._sbl_entry.pack(side="left")
        ttk.Button(sbl_frame, text="Copy", width=5, command=self._copy_sbl).pack(side="left", padx=(4, 0))
        ttk.Style().configure("SBLInvalid.TEntry", foreground="#c62828")
        ttk.Style().configure("SBLWarn.TEntry",    foreground="#e65100")
        self._src_labels["sbl"] = tk.Label(info, text="", font=("Consolas", 8), width=12, anchor="w", relief="flat", bd=0)
        self._src_labels["sbl"].grid(row=3, column=2, sticky="w")

        self._form_lbl = ttk.Label(info, text="", font=("Segoe UI", 8))
        self._form_lbl.grid(row=5, column=0, columnspan=3, pady=(2, 0))

        pf = ttk.LabelFrame(self, text="Laserfiche Navigation", padding=10)
        pf.grid(row=1, column=0, **p, sticky="ew")
        self.path_label = ttk.Label(pf, text="Fill in street info above", foreground="gray",
                                    font=("Consolas", 9), wraplength=340)
        self.path_label.grid(row=0, column=0, sticky="w")
        ttk.Button(pf, text="Copy Path", width=10, command=self._copy_path).grid(row=0, column=1, padx=(8, 0))

        wf = ttk.LabelFrame(self, text="Scan Watchers", padding=8)
        wf.grid(row=2, column=0, **p, sticky="ew")
        wf.columnconfigure(0, weight=1)
        self._watch_rows = ttk.Frame(wf)
        self._watch_rows.grid(row=0, column=0, sticky="ew")
        self._watch_rows.columnconfigure(0, weight=1)
        ttk.Button(wf, text="Add Folder…", command=self._add_watch_folder)\
            .grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._toggle_btns = {}
        self._rebuild_watcher_rows()

        self._staged_frame = ttk.LabelFrame(self, text="Staged Batch", padding=8)
        self._staged_frame.grid(row=3, column=0, **p, sticky="ew")
        self._staged_frame.columnconfigure(0, weight=1)
        self._staged_rows = ttk.Frame(self._staged_frame)
        self._staged_rows.grid(row=0, column=0, sticky="ew")
        self._staged_rows.columnconfigure(0, weight=1)
        self._refresh_staged_panel()

        lf = ttk.LabelFrame(self, text="File Activity", padding=10)
        lf.grid(row=4, column=0, **p, sticky="ew")
        ttk.Label(lf, text="Place official Building Permit face-down on top before scanning.",
                  font=("Segoe UI", 8), foreground="#888888").grid(
                  row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.log_box = tk.Text(lf, height=5, width=34, state="disabled",
                               font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                               relief="flat", cursor="arrow")
        self.log_box.grid(row=1, column=0, sticky="nsew")

        # Live page preview — updated as each page is rendered during OCR
        prev_frame = tk.Frame(lf, bg="#1e1e1e", width=115, height=155)
        prev_frame.grid(row=1, column=1, padx=(8, 0), sticky="n")
        prev_frame.grid_propagate(False)
        self._preview_label = tk.Label(prev_frame, bg="#1e1e1e",
                                       text="no\npreview", fg="#444444",
                                       font=("Consolas", 8))
        self._preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self._preview_label.bind("<Button-1>", self._open_preview_file)
        self._preview_photo = None
        self._preview_path = None

        prog_row = ttk.Frame(lf)
        prog_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_row, variable=self.progress_var,
                                            maximum=100, mode="determinate", length=260)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_pct = ttk.Label(prog_row, text="", font=("Consolas", 8), width=5, anchor="e")
        self.progress_pct.pack(side="left", padx=(6, 0))

        bf = ttk.Frame(self)
        bf.grid(row=5, column=0, **p)
        self.confirm_btn = ttk.Button(bf, text="Confirm & Rename", command=self._confirm_rename,
                                      width=18, state="disabled")
        self.confirm_btn.pack(side="left", padx=6)
        ttk.Button(bf, text="New Permit",     command=self._new_permit,    width=12).pack(side="left", padx=6)
        ttk.Button(bf, text="Re-OCR",         command=self._reocr_staging, width=8).pack(side="left", padx=6)
        ttk.Button(bf, text="Open Staging",   command=self._open_staging,  width=13).pack(side="left", padx=6)

        bf2 = ttk.Frame(self)
        bf2.grid(row=6, column=0, padx=12, pady=(0, 6))
        ttk.Button(bf2, text="History",       command=self._show_history,  width=9).pack(side="left", padx=6)
        ttk.Button(bf2, text="Search",        command=self._show_archive,  width=8).pack(side="left", padx=6)
        ttk.Button(bf2, text="Property",      command=self._show_property, width=9).pack(side="left", padx=6)
        ttk.Button(bf2, text="Show OCR",      command=self._show_ocr,      width=9).pack(side="left", padx=6)
        ttk.Button(bf2, text="API Key",       command=self._set_api_key,   width=8).pack(side="left", padx=6)

        ttk.Label(self, text="Enter = Confirm   ·   N = New Permit   ·   R = Re-OCR   ·   click the preview to open the scan",
                  font=("Segoe UI", 8), foreground="#aaaaaa").grid(
                  row=7, column=0, pady=(0, 2))

        self._stats_var = tk.StringVar()
        ttk.Label(self, textvariable=self._stats_var,
                  font=("Segoe UI", 8), foreground="#888888").grid(
                  row=8, column=0, pady=(0, 6))

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _folder_var(self, folder):
        var = self.folder_active.get(folder)
        if var is None:
            var = tk.BooleanVar(value=True)
            self.folder_active[folder] = var
            var.trace_add("write", lambda *_, f=folder: self._update_toggle_btn(f))
        return var

    def _rebuild_watcher_rows(self):
        for w in self._watch_rows.winfo_children():
            w.destroy()
        self._toggle_btns = {}
        for i, folder in enumerate(self.scan_folders):
            self._folder_var(folder)
            row_f = ttk.Frame(self._watch_rows)
            row_f.grid(row=i, column=0, sticky="ew", pady=2)
            ttk.Label(row_f, text=folder, font=("Consolas", 8), foreground="gray").pack(side="left")
            btn = tk.Button(row_f, width=6, relief="groove", cursor="hand2",
                            command=lambda f=folder: self._toggle_watcher(f))
            btn.pack(side="right", padx=(8, 0))
            tk.Button(row_f, text="✕", width=2, relief="flat", cursor="hand2", fg="#b71c1c",
                      command=lambda f=folder: self._remove_watch_folder(f)).pack(side="right")
            self._toggle_btns[folder] = btn
            self._update_toggle_btn(folder)

    def _add_watch_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder to watch for scans")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder in self.scan_folders:
            self._log(f"[--] Already watching {folder}")
            return
        self.scan_folders.append(folder)
        save_scan_folders(self.scan_folders)
        self._watch_folder(folder)
        self._rebuild_watcher_rows()

    def _remove_watch_folder(self, folder):
        # Non-destructive: stops watching only — the folder and its files stay put
        if folder in self.scan_folders:
            self.scan_folders.remove(folder)
        save_scan_folders(self.scan_folders)
        w = self._watches.pop(folder, None)
        if w:
            try:
                self.observer.unschedule(w)
            except Exception:
                pass
        self.folder_active.pop(folder, None)
        self._log(f"[--] Stopped watching {folder}")
        self._rebuild_watcher_rows()

    def _toggle_watcher(self, folder):
        self.folder_active[folder].set(not self.folder_active[folder].get())

    def _update_toggle_btn(self, folder):
        btn = self._toggle_btns.get(folder)
        if not btn:
            return
        active = self.folder_active[folder].get()
        btn.config(text="ON" if active else "OFF",
                   bg="#4caf50" if active else "#f44336",
                   fg="white")

    _STAGED_TAGS = {"official": ("permit", "#2e7d32"),
                    "application": ("application", "#e65100"),
                    "unknown": ("orange folder", "#92400e")}

    def _refresh_staged_panel(self):
        for w in self._staged_rows.winfo_children():
            w.destroy()
        if not self.staged:
            self._staged_frame.config(text="Staged Batch")
            ttk.Label(self._staged_rows, text="nothing staged",
                      font=("Segoe UI", 8), foreground="#888888").grid(row=0, column=0, sticky="w")
            return
        self._staged_frame.config(text=f"Staged Batch ({len(self.staged)})")
        for i, entry in enumerate(self.staged):
            name = os.path.basename(entry["current"])
            row_f = ttk.Frame(self._staged_rows)
            row_f.grid(row=i, column=0, sticky="ew", pady=1)
            disp = name if len(name) <= 34 else name[:31] + "…"
            ttk.Label(row_f, text=disp, font=("Consolas", 8)).pack(side="left")
            if entry.get("renamed"):
                tag, color = "ready to file", "#2e7d32"
            elif os.path.splitext(name)[1].lower() in _IMAGE_EXTS:
                tag, color = "plans", "#888888"
            else:
                tag, color = self._STAGED_TAGS.get(
                    self.file_class.get(entry["current"], ""), ("", "#888888"))
            tk.Button(row_f, text="✕", width=2, relief="flat", cursor="hand2", fg="#b71c1c",
                      command=lambda e=entry: self._unstage(e)).pack(side="right")
            ttk.Label(row_f, text=tag, font=("Segoe UI", 8),
                      foreground=color).pack(side="right", padx=(8, 4))

    def _unstage(self, entry):
        if self._scanning:
            self._log("[--] Scan in progress — wait for it to finish before removing files")
            return
        name = os.path.basename(entry["current"])
        if entry.get("renamed"):
            if not messagebox.askyesno(
                    "Remove Confirmed File",
                    f"{name} is confirmed and waiting to be filed in Laserfiche.\n"
                    "Remove it from staging anyway?"):
                return
        try:
            if os.path.exists(entry["current"]):
                os.remove(entry["current"])
        except Exception as e:
            self._log(f"[!]  Could not remove {name}: {e}")
            return
        if entry in self.staged:
            self.staged.remove(entry)
        self.file_class.pop(entry["current"], None)
        self._log(f"[--] Removed {name} from the batch (original stays in the scan folder)")
        if not any(not e["renamed"] for e in self.staged):
            self.confirm_btn.config(state="disabled")
        self._refresh_staged_panel()

    def _laserfiche_path(self):
        return laserfiche_path_for(self.street_num.get(), self.street_name.get())

    def _refresh_path(self):
        path = self._laserfiche_path()
        if not path:
            self.path_label.config(text="Fill in street info above", foreground="gray")
        elif not self.street_num.get().strip():
            # ~7% of Yorktown parcels have no street number, and Laserfiche names
            # their leaf folders inconsistently: some kept the orphaned space from
            # the "{number} {street}" template ("\ SAGAMORE AVE."), others didn't
            # ("\DARBY ST."). Both confirmed by hand — there is no rule to infer,
            # so flag it instead of guessing. Do NOT "fix" laserfiche_path_for
            # to add or drop the space; that has been changed in both directions
            # already (87eb301) and neither is right for every parcel.
            self.path_label.config(
                text=path + "\n[!] no street number — the folder may start with a "
                            "space; check the name in Laserfiche",
                foreground="#e65100")
        else:
            self.path_label.config(text=path, foreground="#0055cc")

    def _copy_path(self):
        path = self._laserfiche_path()
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        if self.street_num.get().strip():
            self._log("[OK] Path copied to clipboard")
        else:
            leaf = path.rsplit("\\", 1)[-1]
            self._log(f"[!]  Path copied, but this parcel has no street number — the "
                      f"folder is either '{leaf}' or ' {leaf}' (leading space). "
                      "Both spellings exist in Laserfiche; check before pasting.")

    def _copy_sbl(self):
        sbl = self.sbl.get().strip()
        if sbl:
            self.clipboard_clear()
            self.clipboard_append(sbl)

    def _validate_sbl(self):
        """Red = bad format. Orange = valid format but no such parcel in county data.
        Lots/blocks may carry decimals (5.17-1-18.1) and condos a unit part
        (15.16-1-21.1-2) — all real Yorktown print keys."""
        val = self.sbl.get().strip()
        valid = not val or bool(re.match(
            r'^\d{1,3}\.\d{1,3}-\d{1,3}(?:\.\d{1,3})?-\d{1,3}(?:\.\d{1,3})?(?:-\d{1,3})?$', val))
        style = "TEntry"
        if not valid:
            style = "SBLInvalid.TEntry"
        elif val and parcel_db_available() and not parcel_lookup_sbl(val):
            style = "SBLWarn.TEntry"
        self._sbl_entry.config(style=style)

    def _on_hotkey(self, event):
        if isinstance(self.focus_get(), (ttk.Entry, tk.Entry, tk.Text)):
            return
        if event.keysym == "Return":
            if self.confirm_btn["state"] == "normal":
                self._confirm_rename()
        elif event.char.lower() == "n":
            self._new_permit()
        elif event.char.lower() == "r":
            self._reocr_staging()

    def _update_stats(self):
        history = load_history()
        today    = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        today_n = sum(1 for e in history if e.get("date", "").startswith(today))
        week_n  = sum(1 for e in history if e.get("date", "") >= week_ago)
        self._stats_var.set(
            f"Today: {today_n} permit{'s' if today_n != 1 else ''}   ·   Past 7 days: {week_n}"
        )

    def _update_preview(self, png_bytes, path=None):
        from PIL import Image, ImageTk
        import io
        try:
            img = Image.open(io.BytesIO(png_bytes))
            img.thumbnail((115, 155), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo  # hold reference so GC doesn't collect it
            self._preview_label.config(image=photo, text="",
                                       cursor="hand2" if path else "arrow")
            self._preview_path = path
        except Exception:
            pass

    def _clear_preview(self):
        self._preview_photo = None
        self._preview_path = None
        self._preview_label.config(image="", text="no\npreview", cursor="arrow")

    def _open_preview_file(self, _event=None):
        p = self._preview_path
        if not p:
            return
        if not os.path.exists(p):
            # Confirm & Rename moves the file out from under the thumbnail
            batch = [e["current"] for e in self.staged if os.path.exists(e["current"])]
            if len(batch) == 1:
                p = batch[0]
            else:
                self._log(f"[--] {os.path.basename(p)} is no longer in staging")
                return
        try:
            os.startfile(p)
        except Exception as e:
            self._log(f"[!]  Could not open {os.path.basename(p)}: {e}")

    # ── File handling ─────────────────────────────────────────────────────────

    def _on_file(self, path):
        self.after(0, self._handle_file, path)

    def _handle_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in SKIP_EXTENSIONS:
            return   # half-written scanner/download temp file
        # Only scanner output is ours. Anything else saved into a watched folder
        # used to be copied to staging and handed to fitz, which just errored.
        if ext not in _STAGE_EXTS:
            self._log(f"[--] Ignored {os.path.basename(path)} — not a scan file")
            return

        origin_folder = os.path.dirname(path)

        if not self.folder_active.get(origin_folder, tk.BooleanVar(value=True)).get():
            return  # watcher paused for this folder

        filename = os.path.basename(path)
        staging_path  = os.path.join(STAGING_FOLDER, filename)

        # Avoid collision if a same-named file is already in staging
        if os.path.exists(staging_path):
            base, ext2 = os.path.splitext(filename)
            staging_path = os.path.join(STAGING_FOLDER, f"{base}_{int(time.time())}{ext2}")

        try:
            shutil.copy2(path, staging_path)
        except Exception as e:
            self._log(f"[!]  Could not copy to staging: {e}")
            return

        self.staged.append({"current": staging_path, "renamed": False})
        self._refresh_staged_panel()
        self._log(f"[IN]  {filename}  →  staging (copy)")
        metric("document_ingested", detail=ext)

        # Image files are building plans — staged for the merge, never a data source
        if ext in _IMAGE_EXTS:
            self._log("[--]  Image file staged as building plans — skipped as data source")
            self.confirm_btn.config(state="normal")
            return

        if not self.permit_id.get().strip():
            self._log("[..]  Reading document...")
            threading.Thread(target=self._ocr_and_fill, args=(staging_path,), daemon=True).start()
        else:
            self._log("[--]  Using current permit info")
            self.confirm_btn.config(state="normal")

    def _archive_scan_text(self, doc, path, ocr_text, form_type):
        """Store this file's text in the full-text archive. Native page text is free;
        the OCR'd target-page text is prepended when it isn't already native."""
        try:
            pages = "\n\n".join(t for t in (doc[i].get_text() for i in range(len(doc))) if t.strip())
            arch_text = pages
            if ocr_text.strip() and ocr_text.strip() not in pages:
                arch_text = (ocr_text + "\n\n" + pages).strip()
            archive_record_scan(path, arch_text, form_type)
        except Exception as e:
            self.after(0, self._log, f"[!]  Archive write failed: {e}")

    def _extract_fields(self, path):
        """Run full extraction on a PDF. Returns (permit, num, street, sbl, sources).
        Safe to call from any thread — all UI updates go through self.after."""
        import fitz
        doc = fitz.open(path)
        total_pages = len(doc)
        # The official permit is always the earliest sheet — flip-checks and
        # the full-power fallback never look past the front of the doc.
        flip_window = min(3, total_pages)

        self.after(0, self._set_progress, 5)

        # Pass 1: native text page detection (instant for digital PDFs)
        official_idx = None
        application_idx = None
        for i in range(total_pages):
            ptype = detect_permit_type(doc[i].get_text())
            if ptype == "official" and official_idx is None:
                official_idx = i
            elif ptype == "application" and application_idx is None:
                application_idx = i

        self.after(0, self._set_progress, 20)

        # Pass 2: official not in native text — quick OCR sweep (1.5×, single
        # pass) over every page, front to back. Cheap enough to cover the whole
        # doc; the expensive dual-pass OCR runs only on the chosen target page.
        # Also remembers the first orange-folder page so an unknown batch
        # extracts from the cover sheet wherever it sits, not page 1.
        orange_idx = None
        app_front = False
        if official_idx is None:
            self.after(0, self._log, "[..] No official permit in native text — quick-scanning pages...")
            _wlog = lambda m: self.after(0, self._log, m)
            for i in range(total_pages):
                if len(doc[i].get_text().strip()) > 40:
                    continue   # substantial native text — already classified in Pass 1
                if _plan_sized(doc[i]):
                    # Orange folder scanned flat on the plan scanner: plan-sized,
                    # but still the extraction failsafe — spot it by paper color
                    if orange_idx is None and _orange_cover_page(doc[i]):
                        orange_idx = i
                        self.after(0, self._log, f"[..] Orange folder cover spotted on page {i + 1} (by color)")
                    continue   # plan sheet — never a permit, slow to OCR
                text  = quick_page_text(doc, i)
                ptype = detect_permit_type(text)
                # Unreadable early page: flip-check it before trusting any
                # later page — the official permit is always the earliest sheet
                if ptype == "unknown" and i < flip_window:
                    if try_flip_official(doc, i, quick_ocr_page_type, log=_wlog):
                        ptype = "official"
                self.after(0, self._set_progress, 20 + int(45 * (i + 1) / total_pages))
                if ptype == "official":
                    official_idx = i
                    break
                if ptype == "application":
                    # Prefer the form FRONT (permit-no box / office-use markers)
                    # over other application pages — e.g. the environmental-
                    # review back side, which has no extractable fields
                    is_front = bool(re.search(r'PERMIT\s*N[O0]|OFFICE\s+USE\s+ONLY',
                                              text, re.IGNORECASE))
                    if application_idx is None or (is_front and not app_front):
                        application_idx = i
                        app_front = is_front
                if orange_idx is None and _ORANGE_LABEL_RE.search(text):
                    orange_idx = i

        # Fallback: a faint scan can defeat the quick pass yet still be readable
        # by the full dual-pass OCR — re-check the front pages at full power
        # before concluding there is no official permit. Bounds the worst case
        # at roughly the old behavior plus the quick sweep.
        if official_idx is None:
            _fb_logged = False
            for i in range(flip_window):
                if len(doc[i].get_text().strip()) > 40 or _plan_sized(doc[i]):
                    continue
                if not _fb_logged:
                    self.after(0, self._log, "[..] Quick scan found no permit — retrying first pages at full power...")
                    _fb_logged = True
                ptype = detect_permit_type(extract_text_from_page(doc, i))
                self.after(0, self._set_progress, 65 + int(15 * (i + 1) / flip_window))
                if ptype == "official":
                    official_idx = i
                    break
                if ptype == "application" and application_idx is None:
                    application_idx = i

        file_kind = None
        if official_idx is not None:
            target, permit_type = official_idx, "official"
        elif application_idx is not None and orange_idx is None:
            # Last-resort data source: the application form itself. Handwritten-
            # heavy, so its Tesseract text ranks lowest (tesseract_hw) and Claude
            # Sonnet does the real reading; county reconcile + history gatekeep.
            self.after(0, self._log, "[..] Application form is the only data source — extracting (last resort)")
            target, permit_type = application_idx, "application"
        elif application_idx is not None:
            # Application form AND orange folder cover in the same file (typical
            # merged batch): the application is still no data source, but the
            # cover is — extract from it so the permit ID isn't silently lost.
            self.after(0, self._log,
                       f"[..] Application form + orange folder cover in one file — extracting from the cover (page {orange_idx + 1})")
            target, permit_type = orange_idx, "unknown"
            file_kind = "application"   # keep filing/merge behavior of an application file
        else:
            # No recognizable form anywhere: extract from the orange folder
            # cover if the quick sweep spotted one, else page 1
            target = orange_idx if orange_idx is not None else 0
            permit_type = "unknown"

        self.after(0, self._log, f"[..] Form type: {permit_type} (page {target + 1})")
        self.file_class[path] = file_kind or permit_type

        # Push a quick 1.5× render of the target page to the preview panel
        try:
            import fitz as _fitz
            _prev = doc[target].get_pixmap(matrix=_fitz.Matrix(1.5, 1.5))
            self.after(0, self._update_preview, _prev.tobytes("png"), path)
            del _prev
        except Exception:
            pass

        native_text = doc[target].get_text()
        if len(native_text.strip()) > 80:
            text = native_text
        else:
            text = extract_text_from_page(doc, target, printed=(permit_type == "official"))
        self.last_ocr_text = text
        self._archive_scan_text(doc, path, text, permit_type)

        self.after(0, self._log, f"[..] Text sample: {text[:120].strip()!r}")

        app_no        = find_application_number(text)
        app_no_digits = re.sub(r'\D', '', app_no)
        if app_no:
            self.after(0, self._log, f"[..] Application No. detected: {app_no} — will not use as permit ID")

        permit  = find_permit_number(text, blocked_digits=app_no_digits or None)
        address = find_address(text)
        sbl     = find_sbl(text)

        subtype = ""
        if permit_type == "official":
            sm = _OFFICIAL_PERMIT_TYPES.search(text.upper())
            subtype = sm.group(0).title() if sm else "Building Permit"
        sources = {"permit": "", "address": "", "sbl": "",
                   "form_type": permit_type, "form_page": target + 1, "form_subtype": subtype}
        # Handwritten forms: Tesseract regex is unreliable on handwriting — rank below Claude
        if permit_type in ("application", "unknown"):
            text_source = "tesseract_hw"
        else:
            text_source = "native" if len(native_text.strip()) > 80 else "tesseract"
        if permit:  sources["permit"]  = text_source
        if address: sources["address"] = text_source
        if sbl:     sources["sbl"]     = text_source

        # Claude fills missing fields; for handwritten-heavy forms (orange folder,
        # application) it also overrides tesseract_hw
        handwritten = permit_type in ("unknown", "application")
        claude_needed = not (permit and address and sbl) or handwritten
        if claude_needed and getattr(self, "batch_verified", False):
            self.after(0, self._log,
                       "[..] Skipping Claude — this batch already has a county-verified result")
            claude_needed = False
        if claude_needed:
            api_key = load_claude_key()
            if api_key:
                try:
                    missing = [n for n, v in [("permit", permit), ("address", address), ("sbl", sbl)] if not v]
                    self.after(0, self._set_progress, 85)
                    self.after(0, self._log, f"[..] Sending to Claude (missing: {', '.join(missing)})...")
                    # Handwritten forms need higher zoom + a stronger model to read reliably
                    zoom  = 3.0 if handwritten else 2.0
                    cl_model = "claude-sonnet-4-6" if handwritten else "claude-haiku-4-5-20251001"
                    cl_permit, cl_address, cl_sbl = extract_fields_with_claude(
                        _claude_page_png(doc[target], zoom), api_key, app_no=app_no, model=cl_model)
                    self.after(0, self._log, f"[..] Claude returned permit='{cl_permit}' address='{cl_address}' sbl='{cl_sbl}'")
                    cl_permit_digits = re.sub(r'\D', '', cl_permit)
                    permit_slot_open = not permit or (handwritten and sources["permit"] == "tesseract_hw")
                    if permit_slot_open and cl_permit and '-' not in cl_permit and len(cl_permit_digits) == 8:
                        if app_no_digits and cl_permit_digits == app_no_digits:
                            self.after(0, self._log, f"[!]  Claude returned application number ({cl_permit}) — discarding")
                        else:
                            permit = cl_permit
                            sources["permit"] = "claude"
                    elif (permit and re.fullmatch(r'\d{8}', permit) and '-' not in cl_permit
                          and len(cl_permit_digits) == 8
                          and cl_permit.upper().startswith(permit) and len(cl_permit) > 8):
                        # Same 8 digits, but Claude sees a type suffix (FD, DEMO, ...)
                        # the text read missed — suffixes distinguish real permits
                        # (20160001 vs 20160001FD are different jobs)
                        self.after(0, self._log,
                                   f"[..] Claude read a suffix on the permit: {permit} → {cl_permit.upper()}")
                        permit = cl_permit.upper()
                    addr_slot_open = not address or (handwritten and sources["address"] == "tesseract_hw")
                    if addr_slot_open and cl_address:
                        cl_address = re.sub(r',?\s*(Yorktown|New York|NY|\d{5}).*$', '', cl_address, flags=re.IGNORECASE).strip()
                        suffix_m = re.search(rf'\b({STREET_SUFFIXES})\b\.?', cl_address, re.IGNORECASE)
                        if suffix_m:
                            cl_address = cl_address[:suffix_m.end()].strip().rstrip(',.')
                        address = cl_address
                        sources["address"] = "claude"
                    sbl_slot_open = not sbl or (handwritten and sources["sbl"] == "tesseract_hw")
                    # A held OCR read that resolves to NO county parcel must not
                    # block a Claude read that does — validation beats source
                    # rank. (Native text is exempt: a printed SBL missing from
                    # the roll can be a legitimately renumbered old parcel.)
                    if not sbl_slot_open and cl_sbl and cl_sbl != sbl \
                            and sources["sbl"] in ("tesseract", "tesseract_hw") \
                            and _county_resolves(sbl) is None and _county_resolves(cl_sbl):
                        self.after(0, self._log,
                                   f"[..] OCR SBL '{sbl}' matches no county parcel; Claude's "
                                   f"'{cl_sbl}' does — using Claude's")
                        sbl_slot_open = True
                    if sbl_slot_open and cl_sbl:
                        sbl = cl_sbl
                        sources["sbl"] = "claude"
                except Exception as e:
                    self.after(0, self._log, f"[!]  Claude error: {e}")

        # Fallback tier: electrical certs / plan-review pages elsewhere in this
        # file. Lowest rank, fill-only — nothing here can displace a value found
        # above, and reconcile_with_parcels gatekeeps whatever they contribute.
        if not (permit and address and sbl):
            try:
                fb = _sweep_fallback_pages(doc, target)
                if not address and "address" in fb:
                    val, src, pg = fb["address"]
                    address = val
                    sources["address"] = src
                    _lbl = "electrical cert" if src == "elec_cert" else "plan-review list"
                    self.after(0, self._log, f"[..] Fallback: address '{val}' from {_lbl} (page {pg}) — verify")
                if not sbl and "sbl" in fb:
                    val, src, pg = fb["sbl"]
                    sbl = val
                    sources["sbl"] = src
                    self.after(0, self._log, f"[..] Fallback: SBL '{val}' from electrical cert (page {pg}) — verify")
                if not permit and "permit_hint" in fb:
                    val, pg = fb["permit_hint"]
                    self.after(0, self._log,
                               f"[??] Electrical cert (page {pg}) shows permit '{val}' — SUGGESTION ONLY, "
                               "cert numbers can be one digit off; verify against the documents before entering")
            except Exception as e:
                self.after(0, self._log, f"[!]  Fallback sweep error: {e}")

        # History fill: permit → property memory. Confirmed filings only, exact
        # 8-digit match, single-parcel matches only (ambiguity = abstain), SBL
        # must be a real county parcel, and county reconcile still gatekeeps.
        if permit and (not address or not sbl):
            try:
                hist = history_lookup_permit(permit)
                if len(hist) == 1:
                    h_addr, h_sbl, h_when, _fn = hist[0]
                    when = (h_when or "")[:10]
                    if not sbl and h_sbl and parcel_db_available() and parcel_lookup_sbl(h_sbl):
                        sbl = h_sbl
                        sources["sbl"] = "history"
                        self.after(0, self._log,
                                   f"[..] History: SBL {h_sbl} from prior filing of permit {permit} ({when}) — verify")
                    if not address and h_addr:
                        address = h_addr
                        sources["address"] = "history"
                        self.after(0, self._log,
                                   f"[..] History: address '{h_addr}' from prior filing of permit {permit} ({when}) — verify")
                elif len(hist) > 1:
                    opts = "; ".join(f"{a or '?'} ({s})" for a, s, _w, _f in hist[:4])
                    self.after(0, self._log,
                               f"[!]  History: permit {permit} maps to multiple parcels — not auto-filling: {opts}")
                else:
                    self.after(0, self._log,
                               f"[i]  Permit {permit} has no prior confirmed filing in history — "
                               "county data has no permit numbers, so the address/SBL can't be "
                               "looked up from a permit ID alone")
            except Exception as e:
                self.after(0, self._log, f"[!]  History lookup error: {e}")

        doc.close()
        num, street = split_address(address) if address else ("", "")
        if street:
            corrected = fuzzy_match_street(street)
            if corrected != street:
                self.after(0, self._log, f"[..] Street corrected: '{street}' → '{corrected}'")
            street = corrected
        num, street, sbl, sources = reconcile_with_parcels(
            num, street, sbl, sources, lambda m: self.after(0, self._log, m))

        # Last gate: never leave a value on the form we can PROVE is wrong.
        # Machine reads only — printed (native) values stay, since historical
        # permits legitimately carry renumbered parcels / renamed streets.
        _machine = ("tesseract", "tesseract_hw", "claude", "elec_cert", "plan_review")
        if street and sources.get("address") in _machine \
                and street.upper() not in KNOWN_STREETS \
                and fuzzy_match_street(street) == street:
            self.after(0, self._log,
                       f"[!]  Dropped street '{street}' — not a Yorktown street, no county "
                       "repair found; left blank (check the form or Property lookup)")
            street = ""
            sources["address"] = ""
        if sbl and sources.get("sbl") in _machine and _county_resolves(sbl) is None:
            self.after(0, self._log,
                       f"[!]  Dropped SBL '{sbl}' — matches no county parcel, no repair "
                       "found; left blank (check the form or Property lookup)")
            sbl = ""
            sources["sbl"] = ""
        return permit, num, street, sbl, sources

    def _ocr_and_fill(self, path):
        try:
            permit, num, street, sbl, sources = self._extract_fields(path)
            self.after(0, self._apply_extracted, permit, num, street, sbl, sources)
        except Exception as e:
            self.after(0, self._log, f"[!]  OCR error: {e}")
            self.after(0, lambda: self.confirm_btn.config(state="normal"))

    def _update_src_label(self, field, source):
        lbl = self._src_labels.get(field)
        if not lbl:
            return
        text, color = _SRC_STYLE.get(source, ("", "#888888"))
        lbl.config(text=text, fg=color)

    def _field_edited(self, field):
        if self._prog_set:
            return
        value = {"permit": self.permit_id, "address": self.street_name,
                 "sbl": self.sbl}[field].get().strip()
        # Cleared field goes back to unsourced; manual outranks every extractor,
        # so a late OCR thread can never overwrite what the user typed
        source = "manual" if value else ""
        if self._current_sources.get(field) != source:
            prior = self._current_sources.get(field, "")
            self._current_sources[field] = source
            self._update_src_label(field, source)
            if source == "manual":
                # prior tells us which extractor's value the user replaced
                metric("manual_override", field=field, source=prior)

    def _apply_extracted(self, permit, num, street, sbl, sources=None):
        self._prog_set = True
        try:
            self._apply_extracted_inner(permit, num, street, sbl, sources)
        finally:
            self._prog_set = False

    def _apply_extracted_inner(self, permit, num, street, sbl, sources=None):
        self._set_progress(100)
        sources = sources or {}

        def _cur_rank(field):
            return _SOURCE_RANK.get(self._current_sources.get(field, ""), 0)

        def _new_rank(key):
            return _SOURCE_RANK.get(sources.get(key, ""), 0)

        # Only overwrite a field if the incoming source is strictly better, or the field is empty.
        # Prevents a late-finishing OCR thread from clobbering better data already set.
        permit_wins  = bool(permit)  and (_new_rank("permit")  > _cur_rank("permit")  or not self.permit_id.get().strip())
        address_wins = bool(street)  and (_new_rank("address") > _cur_rank("address") or not self.street_name.get().strip())
        sbl_wins     = bool(sbl)     and (_new_rank("sbl")     > _cur_rank("sbl")     or not self.sbl.get().strip())

        form_type = sources.get("form_type", "")
        form_page = sources.get("form_page", "")
        suffix = f"  ·  page {form_page}" if form_page else ""
        if form_type == "official":
            label_text = sources.get("form_subtype") or "Building Permit"
            self._form_lbl.config(text=label_text + suffix, foreground="#2e7d32")
        elif form_type == "application":
            self._form_lbl.config(text="Permit Application" + suffix, foreground="#e65100")

        if permit_wins:
            self.permit_id.set(permit)
            self._current_sources["permit"] = sources.get("permit", "")
            self._update_src_label("permit", sources.get("permit", ""))
            self._log(f"[OK] Permit ID: {permit}")
            metric("field_filled", field="permit", source=sources.get("permit", ""),
                   doc_class=form_type)
        elif not self.permit_id.get().strip():
            self._log("[!]  Permit ID not found — enter manually")
            metric("extraction_failed", field="permit", doc_class=form_type)

        if address_wins:
            self.street_num.set(num)
            self.street_name.set(street)
            self._current_sources["address"] = sources.get("address", "")
            self._update_src_label("address", sources.get("address", ""))
            if num:
                self._log(f"[OK] Address: {num} {street}")
            else:
                self._log(f"[?]  Address found (check number): {street}")
            metric("field_filled", field="address", source=sources.get("address", ""),
                   doc_class=form_type)
        elif not self.street_name.get().strip():
            self._log("[!]  Address not found — enter manually")
            metric("extraction_failed", field="address", doc_class=form_type)

        self._copy_path()

        if sbl_wins:
            self.sbl.set(sbl)
            self._current_sources["sbl"] = sources.get("sbl", "")
            self._update_src_label("sbl", sources.get("sbl", ""))
            self._log(f"[OK] SBL: {sbl}  (click Copy when ready)")
            metric("field_filled", field="sbl", source=sources.get("sbl", ""),
                   doc_class=form_type)
        elif not self.sbl.get().strip():
            self._log("[!]  SBL not found — enter manually")
            metric("extraction_failed", field="sbl", doc_class=form_type)

        # A complete county-verified triple ends the batch's need for Claude —
        # files that arrive after this point extract with Tesseract only
        metric("parcel_reconcile", doc_class=form_type,
               status="verified" if sources.get("verified") else "unverified")
        if sources.get("verified") and permit and \
                self.permit_id.get().strip() and self.street_name.get().strip() \
                and self.sbl.get().strip():
            self.batch_verified = True

        self._refresh_staged_panel()   # form-type tags may have just been classified
        self.confirm_btn.config(state="normal")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _confirm_rename(self):
        permit = self.permit_id.get().strip()
        address = f"{self.street_num.get().strip()} {self.street_name.get().strip().upper()}".strip()
        sbl = self.sbl.get().strip()

        if not permit:
            self._log("[!]  Enter a Permit ID before confirming")
            return

        # Establish there is actually something to file BEFORE prompting about
        # duplicates — the dialog used to appear even on a no-op confirm.
        unrenamed = [e for e in self.staged if not e["renamed"]]
        if not unrenamed:
            self._log("[!]  Nothing in staging to confirm")
            return
        if self._scanning:
            self._log("[--] Scan in progress — wait for it to finish before confirming")
            return

        # Duplicate detection — archive first (filenames/dates/status), JSON
        # history as fallback. A hit is often legitimate: permits get rescanned
        # as REVISED versions when info changes — so ask, never block.
        try:
            prior_filings = history_prior_filings(permit)
        except Exception:
            prior_filings = []
        if prior_filings:
            metric("duplicate_prompted", detail="archive")
            lines = "\n".join(f"   {fn}   ({(when or '')[:10]}{',  ' + st if st else ''})"
                              for fn, when, st, _lf in prior_filings[:5])
            if not messagebox.askyesno(
                    "Permit Already Filed",
                    f"Permit {permit} is already in the archive:\n\n{lines}\n\n"
                    "File this batch anyway?\n"
                    "(Yes is correct if this is a REVISED version or additional pages.)"):
                return
        else:
            # Exact match only — 20160001, 20160001FD and 20160001DEMO share the
            # first 8 digits but are different permits on different properties
            prior = [e for e in load_history() if e.get("permit_id", "") == permit]
            if prior:
                metric("duplicate_prompted", detail="history")
                last = prior[0]
                if not messagebox.askyesno(
                        "Duplicate Permit",
                        f"Permit {permit} was already filed on {last['date']}.\n"
                        f"Address on file: {last.get('address', '—')}\n\nFile it again?"):
                    return

        new_path = os.path.join(STAGING_FOLDER, f"{permit}.pdf")
        # An earlier filing of this permit may still be sitting in staging waiting
        # to be dragged into Laserfiche — the merge path would have silently
        # replaced it (os.replace) and the single-file path would have failed.
        new_path = self._free_final_path(new_path, unrenamed)
        new_name = os.path.basename(new_path)
        if new_name != f"{permit}.pdf":
            self._log(f"[--] {permit}.pdf is still in staging — "
                      f"filing this batch as {new_name} so nothing is overwritten")
        self.confirm_btn.config(state="disabled")
        lf_path = self._laserfiche_path()

        # Single PDF: plain rename, no rewrite
        if len(unrenamed) == 1 and unrenamed[0]["current"].lower().endswith(".pdf"):
            entry = unrenamed[0]
            old_name = os.path.basename(entry["current"])
            try:
                os.rename(entry["current"], new_path)
            except Exception as e:
                self._log(f"[X]  {old_name}: {e}")
                self.confirm_btn.config(state="normal")
                return
            entry["current"] = new_path
            entry["renamed"] = True
            self._log(f"[OK] {new_name}")
            # A pre-merged batch carries its original filenames — stamp each
            # component's archive row, not the merged name
            pairs = [(c, new_name) for c in entry.get("components", [old_name])]
            self._finish_confirm(pairs, new_path, permit, address, sbl, lf_path)
            return

        # Multiple files (or images): assemble one PDF in filing order
        self._scanning = True
        threading.Thread(target=self._merge_and_finalize,
                         args=(unrenamed, new_path, permit, address, sbl, lf_path),
                         daemon=True).start()

    def _free_final_path(self, new_path, entries):
        """A final name that won't clobber a confirmed file still in staging.

        Overwriting the target is CORRECT when the target is one of our own
        inputs — a re-merge legitimately includes the previously merged file.
        Any other collision is a real earlier filing (typically a REVISED
        rescan of the same permit) and gets the next free ' - 2' name.
        """
        def _key(p):
            return os.path.normcase(os.path.abspath(p))
        inputs = {_key(e["current"]) for e in entries}
        if not os.path.exists(new_path) or _key(new_path) in inputs:
            return new_path
        base, ext = os.path.splitext(new_path)
        n = 2
        while os.path.exists(f"{base} - {n}{ext}"):
            n += 1
        return f"{base} - {n}{ext}"

    def _merge_category(self, path):
        """Bucket a staged file for merge ordering. Image files come from the
        plan scanner; an oversized PDF page is plans unless it's a known
        oversized form (INSPECTION PROCEDURE sheet); orange folder covers are
        spotted by their preprinted labels."""
        if os.path.splitext(path)[1].lower() in _IMAGE_EXTS:
            return "plans"
        ft = self.file_class.get(path)
        if ft is None:
            ft = self._classify_file(path)
            self.file_class[path] = ft
        if ft == "official":
            return "official"
        if ft == "application":
            return "other"
        try:
            import fitz
            doc = fitz.open(path)
            rect = doc[0].rect
            oversized = rect.width * rect.height > _LEGAL_AREA * 1.4
            orange_paper = oversized and _orange_cover_page(doc[0])
            text = quick_page_text(doc, 0)
            doc.close()
        except Exception:
            return "other"
        if oversized and not _INSPECTION_SHEET_RE.search(text):
            return "orange" if orange_paper else "plans"
        if _ORANGE_LABEL_RE.search(text):
            return "orange"
        return "other"

    def _merge_staged(self, entries, new_path):
        """Worker-safe: assemble entries' files into one PDF at new_path in
        filing order (official → other → plans → orange). Deletes merged
        sources. Returns (merged_entries, component_orig_names)."""
        import fitz
        _log = lambda m: self.after(0, self._log, m)
        for e in entries:
            e["_cat"] = self._merge_category(e["current"])
            _log(f"[..] {os.path.basename(e['current'])} → {e['_cat']}")
        entries.sort(key=lambda e: (_MERGE_ORDER[e["_cat"]],
                                    os.path.getctime(e["current"]) if os.path.exists(e["current"]) else 0))
        out = fitz.open()
        merged = []
        for e in entries:
            p = e["current"]
            try:
                src = fitz.open(p)
                if src.is_pdf:
                    out.insert_pdf(src)
                else:
                    out.insert_pdf(fitz.open("pdf", src.convert_to_pdf()))
                src.close()
                merged.append(e)
            except Exception as ex:
                _log(f"[X]  Could not merge {os.path.basename(p)}: {ex} — left in staging, file it separately")
        if not merged:
            out.close()
            return [], []
        # Save via a temp name: a re-merge can have the previous merged file
        # (same target path) among its inputs.
        tmp = new_path + ".tmp"
        out.save(tmp)
        out.close()
        components = []
        for e in merged:
            components.extend(e.get("components", [os.path.basename(e["current"])]))
            try:
                os.remove(e["current"])
            except Exception as ex:
                _log(f"[!]  Could not remove {os.path.basename(e['current'])}: {ex}")
            e["_merged"] = True
        os.replace(tmp, new_path)
        _log(f"[OK] {os.path.basename(new_path)} — {len(merged)} file(s) merged "
             f"({' → '.join(e['_cat'] for e in merged)})")
        return merged, components

    def _swap_staged(self, entries, new_path, components, renamed):
        """UI thread: replace merged staging entries with the single output file."""
        gone = {id(e) for e in entries if e.get("_merged")}
        self.staged[:] = [e for e in self.staged if id(e) not in gone]
        self.staged.append({"current": new_path, "renamed": renamed,
                            "components": components})
        self._refresh_staged_panel()
        self.confirm_btn.config(state="normal")

    def _merge_and_finalize(self, entries, new_path, permit, address, sbl, lf_path):
        """Worker: merge staged files into one ordered PDF, then finish on the UI thread."""
        try:
            merged, components = self._merge_staged(entries, new_path)
            if not merged:
                self.after(0, self._log, "[X]  Merge produced no pages — nothing confirmed")
                self.after(0, lambda: self.confirm_btn.config(state="normal"))
                return
            pairs = [(c, os.path.basename(new_path)) for c in components]
            def _finish():
                self._swap_staged(entries, new_path, components, True)
                self._finish_confirm(pairs, new_path, permit, address, sbl, lf_path)
            self.after(0, _finish)
        except Exception as ex:
            self.after(0, self._log, f"[X]  Merge failed: {ex}")
            self.after(0, lambda: self.confirm_btn.config(state="normal"))
        finally:
            self._scanning = False

    def _finish_confirm(self, renamed_pairs, new_path, permit, address, sbl, lf_path):
        metric("batch_confirmed", detail=str(len(renamed_pairs)))
        append_history(permit, address, sbl)
        try:
            archive_confirm(renamed_pairs, permit, address, sbl, "", lf_path)
        except Exception as e:
            self._log(f"[!]  Archive update failed: {e}")
        self._update_stats()
        self.confirm_btn.config(state="disabled")
        self._refresh_staged_panel()
        self._log(f"[--] {os.path.basename(new_path)} ready — drag from staging to Laserfiche")

    def _new_permit(self):
        unfinished = [e for e in self.staged if not e["renamed"]]
        if unfinished:
            if not messagebox.askyesno("Unprocessed Files",
                    f"{len(unfinished)} file(s) haven't been renamed yet. Start new permit anyway?"):
                return

        # Delete staging copies — originals are still in the scan folders
        for entry in self.staged:
            src = entry["current"]
            if os.path.exists(src):
                try:
                    os.remove(src)
                except Exception as e:
                    self._log(f"[!]  Could not delete {os.path.basename(src)}: {e}")

        self.staged.clear()
        self.file_class.clear()
        self.batch_verified = False
        _new_batch_id()   # telemetry: events from here belong to the next permit
        self._clear_preview()
        self._refresh_staged_panel()
        self._prog_set = True
        try:
            self.permit_id.set("")
            self.street_num.set("")
            self.street_name.set("")
            self.sbl.set("")
        finally:
            self._prog_set = False
        self.confirm_btn.config(state="disabled")
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")
        self.path_label.config(text="Fill in street info above", foreground="gray")
        self._set_progress(0)
        for lbl in self._src_labels.values():
            lbl.config(text="")
        self._form_lbl.config(text="")
        self._current_sources = {"permit": "", "address": "", "sbl": ""}

    def _open_staging(self):
        subprocess.Popen(["explorer", STAGING_FOLDER])

    # ── Info windows ──────────────────────────────────────────────────────────

    def _show_ocr(self):
        win = tk.Toplevel(self)
        win.title("Raw OCR Text")
        win.geometry("600x500")
        txt = tk.Text(win, font=("Consolas", 9), wrap="word")
        sb  = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        txt.insert("1.0", self.last_ocr_text or "No OCR text yet — scan a document first.")
        txt.config(state="disabled")

    def _show_history(self):
        history = load_history()
        win = tk.Toplevel(self)
        win.title("Scan History")
        win.resizable(True, True)
        win.geometry("740x500")
        win.minsize(520, 320)

        # ── Search bar ──
        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Search:").pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=search_var, width=34)
        search_entry.pack(side="left", padx=(4, 0))
        search_entry.focus()

        # ── Treeview ──
        mid = ttk.Frame(win)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        cols    = ("date", "permit_id", "address", "sbl", "status")
        headers = ("Date", "Permit ID", "Address", "SBL", "Status")
        tree = ttk.Treeview(mid, columns=cols, show="headings",
                            height=16, selectmode="extended")

        _sort = {"col": None, "rev": False}

        def _sort_by(col):
            if _sort["col"] == col:
                _sort["rev"] = not _sort["rev"]
            else:
                _sort["col"] = col
                _sort["rev"] = False
            for c, h in zip(cols, headers):
                arrow = (" ▲" if not _sort["rev"] else " ▼") if c == _sort["col"] else ""
                tree.heading(c, text=h + arrow)
            _populate()

        for col, hdr in zip(cols, headers):
            tree.heading(col, text=hdr, command=lambda c=col: _sort_by(c))
        tree.column("date",      width=130, minwidth=100, stretch=False)
        tree.column("permit_id", width=110, minwidth=90,  stretch=False)
        tree.column("address",   width=240, minwidth=150)
        tree.column("sbl",       width=110, minwidth=80,  stretch=False)
        tree.column("status",    width=70,  minwidth=60,  stretch=False)

        sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        # ── Footer: status + buttons ──
        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=8, pady=(2, 8))

        status_var = tk.StringVar()
        ttk.Label(foot, textvariable=status_var,
                  font=("Segoe UI", 8), foreground="gray").pack(side="left")

        def _load_entry(event=None):
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            if not vals[1]:
                return
            num, street = split_address(vals[2]) if vals[2] else ("", "")
            self._prog_set = True
            try:
                self.permit_id.set(vals[1])
                self.street_num.set(num)
                self.street_name.set(street)
                self.sbl.set(vals[3])
            finally:
                self._prog_set = False
            self._current_sources = {"permit": "", "address": "", "sbl": ""}
            for lbl in self._src_labels.values():
                lbl.config(text="")
            self._form_lbl.config(text="")
            self._refresh_path()
            win.destroy()

        def _delete_selected():
            sel = tree.selection()
            if not sel:
                return
            keys = {(tree.item(i, "values")[0], tree.item(i, "values")[1])
                    for i in sel if tree.item(i, "values")[1]}
            if not keys:
                return
            n = len(keys)
            if not messagebox.askyesno(
                    "Delete Records",
                    f"Delete {n} selected record{'s' if n > 1 else ''}?\nThis cannot be undone.",
                    parent=win):
                return
            history[:] = [e for e in history
                          if (e.get("date", ""), e.get("permit_id", "")) not in keys]
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
            _populate()

        def _clear_all():
            if not history:
                return
            n = len(history)
            if not messagebox.askyesno(
                    "Clear All History",
                    f"Delete all {n} record{'s' if n != 1 else ''}?\nThis cannot be undone.",
                    parent=win):
                return
            history.clear()
            with open(HISTORY_FILE, "w") as f:
                json.dump([], f)
            _populate()

        ttk.Button(foot, text="Clear All",       command=_clear_all      ).pack(side="right", padx=(4, 0))
        ttk.Button(foot, text="Delete Selected", command=_delete_selected ).pack(side="right", padx=(4, 0))
        ttk.Button(foot, text="Load Selected",   command=_load_entry      ).pack(side="right", padx=(4, 0))

        # ── Population (called on search change, sort, and after delete) ──
        def _populate():
            q = search_var.get().strip().upper()
            rows = [e for e in history
                    if not q or any(q in str(e.get(k, "")).upper() for k in cols)]
            if _sort["col"]:
                rows.sort(key=lambda e: e.get(_sort["col"], ""), reverse=_sort["rev"])
            tree.delete(*tree.get_children())
            for entry in rows:
                tree.insert("", "end", values=(
                    entry.get("date", ""), entry.get("permit_id", ""),
                    entry.get("address", ""), entry.get("sbl", ""),
                    entry.get("status", ""),
                ))
            shown = len(rows)
            total = len(history)
            if total == 0:
                status_var.set("No history yet")
            elif q:
                status_var.set(f"{shown} of {total} records match")
            else:
                status_var.set(f"{total} record{'s' if total != 1 else ''}")

        search_var.trace_add("write", lambda *_: _populate())
        _populate()

        tree.bind("<Double-1>", _load_entry)
        tree.bind("<Delete>",   lambda e: _delete_selected())

    def _show_archive(self):
        """Full-text search across every scan ever archived — searches the OCR text
        itself, not just the filed metadata."""
        win = tk.Toplevel(self)
        win.title("Search Archive — full text of every scan")
        win.resizable(True, True)
        win.geometry("920x580")
        win.minsize(640, 400)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Search:").pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=search_var, width=40)
        search_entry.pack(side="left", padx=(4, 0))
        search_entry.focus()
        ttk.Label(top, text="matches permit ID, address, SBL, and the scanned text",
                  font=("Segoe UI", 8), foreground="#888888").pack(side="left", padx=(8, 0))

        mid = ttk.Frame(win)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        cols    = ("date", "permit_id", "address", "sbl", "status", "file", "match")
        headers = ("Date", "Permit ID", "Address", "SBL", "Status", "File", "Match")
        tree = ttk.Treeview(mid, columns=cols, show="headings", height=12, selectmode="browse")
        for col, hdr in zip(cols, headers):
            tree.heading(col, text=hdr)
        tree.column("date",      width=120, minwidth=100, stretch=False)
        tree.column("permit_id", width=100, minwidth=85,  stretch=False)
        tree.column("address",   width=180, minwidth=120, stretch=False)
        tree.column("sbl",       width=100, minwidth=80,  stretch=False)
        tree.column("status",    width=60,  minwidth=55,  stretch=False)
        tree.column("file",      width=130, minwidth=90,  stretch=False)
        tree.column("match",     width=200, minwidth=120)

        sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        detail = tk.Text(win, height=10, wrap="word", font=("Consolas", 9),
                         bg="#1e1e1e", fg="#d4d4d4", relief="flat", state="disabled")
        detail.pack(fill="both", expand=False, padx=8, pady=(2, 4))

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=8, pady=(0, 8))
        status_var = tk.StringVar()
        ttk.Label(foot, textvariable=status_var, foreground="#888888").pack(side="left")

        def _selected_record():
            sel = tree.selection()
            return archive_get(int(sel[0])) if sel else None

        def _copy_lf_path():
            rec = _selected_record()
            if rec and rec["lf_path"]:
                self.clipboard_clear()
                self.clipboard_append(rec["lf_path"])
                status_var.set(f"Copied: {rec['lf_path']}")
            else:
                status_var.set("No Laserfiche path on this record")

        def _load_into_form():
            rec = _selected_record()
            if not rec:
                return
            num, street = split_address(rec["address"]) if rec["address"] else ("", "")
            self._prog_set = True
            try:
                if rec["permit_id"]:
                    self.permit_id.set(rec["permit_id"])
                if street:
                    self.street_num.set(num)
                    self.street_name.set(street)
                if rec["sbl"]:
                    self.sbl.set(rec["sbl"])
            finally:
                self._prog_set = False
            status_var.set("Loaded into main form")

        ttk.Button(foot, text="Copy LF Path",  command=_copy_lf_path,   width=13).pack(side="right", padx=4)
        ttk.Button(foot, text="Load Into Form", command=_load_into_form, width=14).pack(side="right", padx=4)

        def _show_detail(_event=None):
            rec = _selected_record()
            detail.config(state="normal")
            detail.delete("1.0", "end")
            if rec:
                hdr = (f"Permit: {rec['permit_id'] or '—'}   Address: {rec['address'] or '—'}   "
                       f"SBL: {rec['sbl'] or '—'}   Status: {rec['status'] or '—'}\n"
                       f"Scanned: {rec['scanned_at'] or '—'}   Filed: {rec['confirmed_at'] or '—'}   "
                       f"File: {rec['final_name'] or rec['orig_name'] or '—'}\n"
                       f"Laserfiche: {rec['lf_path'] or '—'}\n"
                       + "─" * 100 + "\n")
                detail.insert("1.0", hdr + (rec["text"] or "(no text captured — pre-archive record)"))
            detail.config(state="disabled")

        def _populate(*_):
            try:
                rows = archive_search(search_var.get())
            except Exception as e:
                status_var.set(f"Search error: {e}")
                return
            tree.delete(*tree.get_children())
            for r in rows:
                rid, scanned, permit, address, sbl, status, final, orig, snip = r
                snip = re.sub(r"\s+", " ", (snip or "")).strip()
                tree.insert("", "end", iid=str(rid), values=(
                    scanned or "", permit or "", address or "", sbl or "",
                    status or "", final or orig or "", snip))
            n = len(rows)
            q = search_var.get().strip()
            status_var.set(f"{n} match{'es' if n != 1 else ''}" if q
                           else f"{n} most recent scan(s) — type to search")
            _show_detail()

        search_var.trace_add("write", _populate)
        tree.bind("<<TreeviewSelect>>", _show_detail)
        _populate()

    def _show_property(self):
        """Property history lookup: address / SBL / street / owner → parcel info,
        every scan on file, and the Laserfiche path."""
        win = tk.Toplevel(self)
        win.title("Property Lookup")
        win.resizable(True, True)
        win.geometry("860x600")
        win.minsize(620, 440)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Property:").pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=search_var, width=36)
        search_entry.pack(side="left", padx=(4, 0))
        search_entry.focus()
        ttk.Label(top, text="address, SBL, street, or owner name",
                  font=("Segoe UI", 8), foreground="#888888").pack(side="left", padx=(8, 0))

        # ── Parcel matches ──
        pf = ttk.LabelFrame(win, text="Parcels (county data)", padding=4)
        pf.pack(fill="both", expand=True, padx=8, pady=4)
        pcols = ("sbl", "address", "owner")
        ptree = ttk.Treeview(pf, columns=pcols, show="headings", height=7, selectmode="browse")
        for c, h, w in (("sbl", "SBL", 130), ("address", "Address", 240), ("owner", "Owner", 260)):
            ptree.heading(c, text=h)
            ptree.column(c, width=w, minwidth=80, stretch=(c == "owner"))
        psb = ttk.Scrollbar(pf, orient="vertical", command=ptree.yview)
        ptree.configure(yscrollcommand=psb.set)
        ptree.grid(row=0, column=0, sticky="nsew")
        psb.grid(row=0, column=1, sticky="ns")
        pf.columnconfigure(0, weight=1)
        pf.rowconfigure(0, weight=1)

        # ── Scans on file for the selected parcel ──
        sf = ttk.LabelFrame(win, text="Scans on file", padding=4)
        sf.pack(fill="both", expand=True, padx=8, pady=4)
        scols = ("date", "permit_id", "status", "file", "sbl")
        stree = ttk.Treeview(sf, columns=scols, show="headings", height=7, selectmode="browse")
        for c, h, w in (("date", "Date", 120), ("permit_id", "Permit ID", 100),
                        ("status", "Status", 70), ("file", "File", 200), ("sbl", "SBL", 110)):
            stree.heading(c, text=h)
            stree.column(c, width=w, minwidth=60, stretch=(c == "file"))
        ssb = ttk.Scrollbar(sf, orient="vertical", command=stree.yview)
        stree.configure(yscrollcommand=ssb.set)
        stree.grid(row=0, column=0, sticky="nsew")
        ssb.grid(row=0, column=1, sticky="ns")
        sf.columnconfigure(0, weight=1)
        sf.rowconfigure(0, weight=1)

        path_var = tk.StringVar(value="Select a parcel above")
        ttk.Label(win, textvariable=path_var, font=("Consolas", 9),
                  foreground="#555555", wraplength=820).pack(fill="x", padx=10)

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=8, pady=(4, 8))
        status_var = tk.StringVar()
        ttk.Label(foot, textvariable=status_var, foreground="#888888").pack(side="left")

        def _selected_parcel():
            sel = ptree.selection()
            if not sel:
                return None
            v = ptree.item(sel[0], "values")
            return {"sbl": v[0], "address": v[1], "owner": v[2]}

        def _lf_path_for_selected():
            p = _selected_parcel()
            if not p or not p["address"]:
                return ""
            m = re.match(r'^(\d+)\s+(.+)$', p["address"])
            if m:
                return laserfiche_path_for(m.group(1), m.group(2))
            return laserfiche_path_for("", p["address"])

        def _copy_lf():
            path = _lf_path_for_selected()
            if path:
                self.clipboard_clear()
                self.clipboard_append(path)
                status_var.set("Laserfiche path copied")
            else:
                status_var.set("No address on this parcel — no path to copy")

        def _use_property():
            p = _selected_parcel()
            if not p:
                return
            m = re.match(r'^(\d+)\s+(.+)$', p["address"] or "")
            self._prog_set = True
            try:
                if m:
                    self.street_num.set(m.group(1))
                    self.street_name.set(m.group(2))
                self.sbl.set(p["sbl"])
            finally:
                self._prog_set = False
            self._current_sources["address"] = "parcel"
            self._current_sources["sbl"] = "parcel"
            self._update_src_label("address", "parcel")
            self._update_src_label("sbl", "parcel")
            status_var.set("Filled into main form (county-verified)")

        ttk.Button(foot, text="Copy LF Path",      command=_copy_lf,      width=13).pack(side="right", padx=4)
        ttk.Button(foot, text="Use This Property", command=_use_property, width=17).pack(side="right", padx=4)

        def _fill_scans(_event=None):
            stree.delete(*stree.get_children())
            p = _selected_parcel()
            if not p:
                path_var.set("Select a parcel above")
                return
            path_var.set(_lf_path_for_selected() or "(no address — no Laserfiche path)")
            try:
                rows = archive_scans_for_parcel(p["sbl"], p["address"])
            except Exception as e:
                status_var.set(f"Archive error: {e}")
                return
            for r in rows:
                rid, scanned, permit, address, sbl, status, final, orig, _snip = r
                stree.insert("", "end", iid=str(rid), values=(
                    scanned or "", permit or "", status or "", final or orig or "", sbl or ""))
            n = len(rows)
            owner = f"   ·   Owner: {p['owner']}" if p["owner"] else ""
            status_var.set(f"{n} scan(s) on file for {p['sbl']}{owner}")

        def _populate(*_):
            ptree.delete(*ptree.get_children())
            stree.delete(*stree.get_children())
            path_var.set("Select a parcel above")
            q = search_var.get()
            if not q.strip():
                status_var.set("Type an address, SBL, street, or owner to look up a property")
                return
            try:
                rows = parcel_search(q)
            except Exception as e:
                status_var.set(f"Lookup error: {e}")
                return
            for i, (pk, addr, owner) in enumerate(rows):
                ptree.insert("", "end", iid=f"p{i}", values=(pk, addr or "", owner or ""))
            status_var.set(f"{len(rows)} parcel(s) match")
            if len(rows) == 1:
                ptree.selection_set("p0")

        search_var.trace_add("write", _populate)
        ptree.bind("<<TreeviewSelect>>", _fill_scans)
        _populate()

    def _set_api_key(self):
        current = load_claude_key()
        win = tk.Toplevel(self)
        win.title("Anthropic API Key")
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, text="Anthropic API key (saved to config file):").pack(padx=16, pady=(16, 4))
        entry = ttk.Entry(win, width=52, show="*")
        entry.insert(0, current)
        entry.pack(padx=16, pady=4)
        def _save():
            key = entry.get().strip()
            if key:
                save_claude_key(key)
                self._log("[OK] API key saved — Claude will be used for next scan")
            win.destroy()
        ttk.Button(win, text="Save", command=_save).pack(pady=(4, 16))
        win.wait_window()

    def _recover_staging(self):
        if not os.path.exists(STAGING_FOLDER):
            return
        files = [os.path.join(STAGING_FOLDER, f) for f in os.listdir(STAGING_FOLDER)
                 if f.lower().endswith(_STAGE_EXTS)]
        pending = 0
        for p in files:
            if any(e["current"] == p for e in self.staged):
                continue
            name = os.path.basename(p)
            # A file already named "{permit} OPEN/CLOSED.pdf" was confirmed in an
            # earlier session and is only waiting to be dragged into Laserfiche.
            # Recovering it as unrenamed would fold last batch's filed PDF into
            # the next Confirm & Rename.
            done = bool(_FINAL_NAME_RE.match(name))
            self.staged.append({"current": p, "renamed": done})
            if done:
                self._log(f"[--] {name} — already confirmed, waiting to be filed")
            else:
                self._log(f"[..] Recovered: {name}")
                pending += 1
        if pending:
            self.confirm_btn.config(state="normal")
            self._log(f"[--] {pending} unfiled file(s) found in staging from previous session")
        if files:
            self._refresh_staged_panel()

    def _reocr_staging(self):
        if self._scanning:
            self._log("[--] Scan already in progress — please wait")
            return
        all_pdfs = sorted(
            [os.path.join(STAGING_FOLDER, f) for f in os.listdir(STAGING_FOLDER)
             if f.lower().endswith(".pdf")],
            key=os.path.getctime  # oldest first — first file to arrive in staging is tried first
        )
        images = [os.path.join(STAGING_FOLDER, f) for f in os.listdir(STAGING_FOLDER)
                  if f.lower().endswith(_IMAGE_EXTS)]
        if not all_pdfs and not images:
            self._log("[!]  No files in staging folder")
            return
        # A file already named "{permit} OPEN/CLOSED.pdf" belongs to a batch that
        # was confirmed already. Re-OCRing it would let the previous permit win
        # this batch's fields, so mark it done and leave it out of the scan set.
        confirmed = [p for p in all_pdfs if _FINAL_NAME_RE.match(os.path.basename(p))]
        pdfs = [p for p in all_pdfs if p not in confirmed]
        for p in confirmed:
            if not any(e["current"] == p for e in self.staged):
                self.staged.append({"current": p, "renamed": True})
            self._log(f"[--] Skipping {os.path.basename(p)} — already confirmed "
                      "(New Permit clears staging)")
        # Register any untracked files (drag-dropped, not from watcher).
        # Images are staged for the merge but are never a data source.
        for p in pdfs + images:
            if not any(e["current"] == p for e in self.staged):
                self.staged.append({"current": p, "renamed": False})
                self._log(f"[..] Registered: {os.path.basename(p)}")
        if images:
            self.confirm_btn.config(state="normal")
        self._refresh_staged_panel()
        if not pdfs:
            self._log("[--] No unfiled PDFs staged — nothing to OCR")
            return
        self._scanning = True
        self._set_progress(0)
        self._log(f"[..] Re-OCR: classifying and scanning {len(pdfs)} file(s)...")
        def _run_reocr():
            try:
                self._reocr_scan_loop(pdfs)
            finally:
                self._scanning = False
                # classification tags are known now — repaint the batch panel
                self.after(0, self._refresh_staged_panel)
        threading.Thread(target=_run_reocr, daemon=True).start()

    def _classify_file(self, path) -> str:
        """Detect form type via native text then fast single-pass OCR. No field extraction."""
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        except Exception:
            pass
        try:
            import fitz
            doc = fitz.open(path)
            total_pages = len(doc)
            flip_window = min(3, total_pages)
            fallback = "unknown"
            # Native text pass — instant for digital PDFs
            for i in range(total_pages):
                pt = detect_permit_type(doc[i].get_text())
                if pt == "official":
                    doc.close()
                    return "official"
                elif pt == "application":
                    fallback = "application"
            if fallback == "application":
                doc.close()
                return "application"
            # Quick single-pass OCR — cheaper than full dual-pass used during
            # extraction. Plan-sized pages are never permits — skipped. An
            # unreadable early page gets flip-checked immediately: a flipped
            # official on page 1 must win over anything on later pages.
            _wlog = lambda m: self.after(0, self._log, m)
            for i in range(total_pages):
                if len(doc[i].get_text().strip()) > 40 or _plan_sized(doc[i]):
                    continue
                pt = quick_ocr_page_type(doc, i)
                if pt == "unknown" and i < flip_window:
                    if try_flip_official(doc, i, quick_ocr_page_type, log=_wlog):
                        doc.close()
                        return "official"
                if pt == "official":
                    doc.close()
                    return "official"
                elif pt == "application":
                    fallback = "application"
            doc.close()
            return fallback
        except Exception:
            return "unknown"

    def _reocr_scan_loop(self, pdfs):
        """Two-phase cascade: classify all files first, then extract in priority order.

        Priority: official Building Permit → Permit Application → orange folder / other.
        Between tiers, stop early if all three fields (permit, address, SBL) are already satisfied.
        Within each tier, source rank governs which result wins when multiple files contribute.
        """
        _TIER_LABEL = {
            "official":    "Building Permit",
            "application": "Permit Application",
            "unknown":     "folder / other",
        }

        # ── Phase 1: Classify ──────────────────────────────────────────────────
        self.after(0, self._log, f"[..] Classifying {len(pdfs)} file(s)...")
        buckets: dict[str, list] = {"official": [], "application": [], "unknown": []}
        for path in pdfs:
            _t0 = time.time()
            ft = self._classify_file(path)
            metric("document_classified", doc_class=ft,
                   duration_ms=int((time.time() - _t0) * 1000))
            self.file_class[path] = ft
            buckets[ft].append(path)
            self.after(0, self._log, f"[..] {os.path.basename(path)} → {_TIER_LABEL[ft]}")

        # Application forms are staged for filing but not used as a data source.
        # They still run as the LAST tier: a merged file classified "application"
        # can carry an orange folder cover inside, and _extract_fields will pull
        # data from that cover if fields are still missing.
        for path in buckets["application"]:
            self.after(0, self._log, f"[--] {os.path.basename(path)} — application form, last-resort tier only")

        # ── Phase 2: Extract in tier order — official first, orange folder fills gaps ──
        best_permit = "";  best_permit_rank = 0
        best_num    = "";  best_street = "";  best_addr_rank = 0
        best_sbl    = "";  best_sbl_rank  = 0
        best_form_type = "";  best_form_page = 0
        best_sources = {"permit": "", "address": "", "sbl": ""}

        for tier in ("official", "unknown", "application"):
            if not buckets[tier]:
                continue

            missing = [n for n, v in [("permit", best_permit),
                                       ("address", best_street),
                                       ("sbl",     best_sbl)] if not v]
            if not missing:
                self.after(0, self._log, f"[OK] All fields complete — skipping {_TIER_LABEL[tier]} tier")
                break

            self.after(0, self._log,
                       f"[..] {_TIER_LABEL[tier]} tier — looking for: {', '.join(missing)}")

            for path in buckets[tier]:
                still_missing = [n for n, v in [("permit", best_permit),
                                                  ("address", best_street),
                                                  ("sbl",     best_sbl)] if not v]
                if not still_missing:
                    break  # all done within this tier

                self.after(0, self._log, f"[..] Trying: {os.path.basename(path)}")
                try:
                    permit, num, street, sbl, sources = self._extract_fields(path)
                    updates = []

                    permit_rank = _SOURCE_RANK.get(sources["permit"], 0)
                    if permit and permit_rank > best_permit_rank:
                        best_permit = permit; best_permit_rank = permit_rank
                        best_sources["permit"] = sources["permit"]
                        updates.append(f"permit={permit}")

                    addr_rank = _SOURCE_RANK.get(sources["address"], 0)
                    if street and (not best_street or addr_rank > best_addr_rank or
                                   (addr_rank == best_addr_rank and len(num) > len(best_num))):
                        best_num = num; best_street = street; best_addr_rank = addr_rank
                        best_sources["address"] = sources["address"]
                        updates.append(f"address={num} {street}")

                    sbl_rank = _SOURCE_RANK.get(sources["sbl"], 0)
                    if sbl and sbl_rank > best_sbl_rank:
                        best_sbl = sbl; best_sbl_rank = sbl_rank
                        best_sources["sbl"] = sources["sbl"]
                        updates.append(f"sbl={sbl}")

                    if not best_form_type:
                        best_form_type = sources.get("form_type", "")
                        best_form_page = sources.get("form_page", 0)

                    if updates:
                        self.after(0, self._log, f"[..] Got: {', '.join(updates)}")
                    else:
                        self.after(0, self._log, f"[--] Nothing new from {os.path.basename(path)}")
                except Exception as e:
                    self.after(0, self._log, f"[!]  Error on {os.path.basename(path)}: {e}")

        # Never end quietly without a permit ID — say so, and say why Claude
        # couldn't be used if that was the reason.
        if not best_permit:
            if not load_claude_key():
                self.after(0, self._log,
                           "[!]  PERMIT ID NOT FOUND — no API key set, so orange covers were "
                           "never sent to Claude. Set one via the API Key button and Re-OCR.")
            else:
                self.after(0, self._log,
                           "[!]  PERMIT ID NOT FOUND after all tiers — check the pages and enter it manually before confirming")
        else:
            # Early duplicate heads-up — Confirm asks again, but say it now
            try:
                _prior = history_prior_filings(best_permit)
                if _prior:
                    _fn, _when, _st, _lf = _prior[0]
                    self.after(0, self._log,
                               f"[i]  Permit {best_permit} was filed before: {_fn} ({(_when or '')[:10]}) — "
                               "expected if this batch is a REVISED version")
            except Exception:
                pass

        best_sources["form_type"] = best_form_type
        best_sources["form_page"] = best_form_page
        # Final cross-check on the merged result — catches an address from one
        # file conflicting with an SBL from another (per-file reconcile can't).
        best_num, best_street, best_sbl, best_sources = reconcile_with_parcels(
            best_num, best_street, best_sbl, best_sources,
            lambda m: self.after(0, self._log, m))
        if best_permit or best_street or best_sbl:
            self.after(0, self._apply_extracted,
                       best_permit, best_num, best_street, best_sbl, best_sources)
        else:
            self.after(0, self._log, "[!]  No data found in any staged file — fill manually")
            self.after(0, lambda: self.confirm_btn.config(state="normal"))

    def _set_progress(self, pct):
        self.progress_var.set(pct)
        self.progress_pct.config(text=f"{int(pct)}%" if pct > 0 else "")

    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        with open(DEBUG_LOG, "a", encoding="utf-8") as _f:
            _f.write(msg + "\n")

    # ── Watchers ──────────────────────────────────────────────────────────────

    def _start_watchers(self):
        self._scan_handler = ScanHandler(self._on_file)
        self.observer = Observer()
        self._watches = {}
        for folder in self.scan_folders:
            self._watch_folder(folder)
        self.observer.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _watch_folder(self, folder):
        if os.path.exists(folder):
            self._watches[folder] = self.observer.schedule(self._scan_handler, folder, recursive=False)
            self._log(f"[--] Watching {folder}")
        else:
            self._log(f"[!]  Not found: {folder}")

    def _restore_window_pos(self):
        pos = load_window_pos()
        m = re.match(r'^([+-]\d+)([+-]\d+)$', pos) if pos else None
        if not m:
            return
        x, y = int(m.group(1)), int(m.group(2))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # Generous bounds so a second monitor still counts, but a position from
        # a since-unplugged screen can't strand the window out of reach
        if -sw <= x <= sw * 2 and 0 <= y <= sh - 80:
            self.geometry(f"{x:+d}{y:+d}")

    def _on_close(self):
        try:
            save_window_pos(f"{self.winfo_x():+d}{self.winfo_y():+d}")
        except Exception:
            pass
        self.observer.stop()
        self.observer.join()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
