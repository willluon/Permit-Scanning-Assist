import tkinter as tk
from tkinter import ttk, messagebox
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
SCAN_FOLDERS   = [r"U:\Documents\Scans", r"F:\scan"]
STAGING_FOLDER = os.path.join(_HOME, "Documents", "Permit Staging")
HISTORY_FILE   = os.path.join(_HOME, "permit_scan_history.json")
CONFIG_FILE    = os.path.join(_HOME, "permit_scan_config.json")
DEBUG_LOG      = os.path.join(_HOME, "permit_scan_debug.log")
SKIP_EXTENSIONS   = {".tmp", ".part", ".crdownload", ""}
TESSERACT_PATH    = os.path.join(_HOME, "AppData", "Local", "Programs", "Tesseract-OCR", "tesseract.exe")
STREET_LIST_FILE  = os.path.join(_HOME, "yorktown_streets.txt")

IGNORE_ADDRESSES = ["363 UNDERHILL AVE"]

# Source tracking
# tesseract_hw = Tesseract on handwriting — less reliable than Claude for numeric fields
# parcel       = county GIS parcel data (yorktown_parcels.db) — authoritative, outranks everything
_SOURCE_RANK = {"parcel": 5, "native": 4, "tesseract": 3, "claude": 2, "tesseract_hw": 1, "": 0}
_SRC_STYLE   = {
    "parcel":       ("county",     "#00695c"),
    "native":       ("text",       "#2e7d32"),
    "tesseract":    ("ocr",        "#e65100"),
    "claude":       ("ai  ←verify","#1565c0"),
    "tesseract_hw": ("ocr?",       "#92400e"),
}

def _load_known_streets():
    if not os.path.exists(STREET_LIST_FILE):
        return []
    with open(STREET_LIST_FILE) as f:
        return [ln.strip().upper() for ln in f if ln.strip()]

KNOWN_STREETS = _load_known_streets()


def fuzzy_match_street(street_name):
    """Snap an OCR/AI street name to the closest known Yorktown street (cutoff 0.8)."""
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

def detect_permit_type(text):
    upper = text.upper()
    # Application check FIRST — application forms contain "BUILDING PERMIT" + Yorktown markers
    # which would otherwise match the official check below.
    if re.search(r'APPLICATION\s+FOR\s+(?:A\s+)?BUILDING\s+PERMIT', upper):
        return "application"
    if _OFFICIAL_PERMIT_TYPES.search(upper) and (
        re.search(r'TOWN\s+OF\s+YORKTOWN', upper) or
        re.search(r'BUILDING\s+DEPARTMENT', upper) or
        '363 UNDERHILL' in upper
    ):
        return "official"
    return "unknown"



def extract_text_from_page(doc, page_idx):
    """Tesseract OCR a single already-open fitz page; combines native + OCR text.
    PyMuPDF applies PDF page rotation automatically when rendering — do NOT re-rotate."""
    import fitz
    import pytesseract
    from PIL import Image
    import io
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    page = doc[page_idx]
    native = page.get_text().strip()
    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
    png_bytes = pix.tobytes("png")

    # Pass A: grayscale only — best for clean printed scans
    img_gray = Image.open(io.BytesIO(png_bytes)).convert("L")
    ocr_a = ocr_image(img_gray, pytesseract)

    # Pass B: contrast-enhanced — better for faint handwritten text
    img_proc = preprocess_for_ocr(Image.open(io.BytesIO(png_bytes)))
    ocr_b = ocr_image(img_proc, pytesseract)

    ocr = ocr_a if _ascii_score(ocr_a) >= _ascii_score(ocr_b) else ocr_b
    return (native + "\n" + ocr).strip()


def preprocess_for_ocr(img):
    from PIL import ImageEnhance, ImageFilter
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _ascii_score(t):
    return sum(1 for c in t if c.isascii() and (c.isalnum() or c in ' \n.,:-#/'))


def ocr_image(img, pytesseract):
    cfg3  = "--oem 1 --psm 3"   # LSTM + auto layout detection (good for forms)
    cfg6  = "--oem 1 --psm 6"   # LSTM + uniform block
    cfg11 = "--oem 1 --psm 11"  # LSTM + sparse/handwritten layout
    results = [pytesseract.image_to_string(img, config=c) for c in [cfg3, cfg6, cfg11]]
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


_PERMIT_ID_SUFFIXES = re.compile(r'\s*(DEMO|RES|COM|ALT|ADD|NEW|POOL|ELEC|PLMB|MECH)\b', re.IGNORECASE)

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
                # Check for a known permit-type suffix immediately after the matched digits
                sm = _PERMIT_ID_SUFFIXES.match(text[m.end():m.end() + 8])
                suffix = sm.group(1).upper() if sm else ""
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
    """Match an as-read SBL against county data. Returns (print_key, addr) or None."""
    for cand in _sbl_candidates(sbl):
        rows = _parcel_query("SELECT print_key, addr FROM parcels WHERE print_key=?", (cand,))
        if rows:
            return rows[0]
    return None


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
        return num, street, sbl, sources

    addr_hit = parcel_lookup_address(num, street) if (num and street) else None
    if addr_hit and _norm_street_key(street) != addr_hit[1]:
        log(f"[..] Street corrected from county data: '{street}' → '{addr_hit[1]}'")
        street = addr_hit[1]

    sbl_hit = parcel_lookup_sbl(sbl) if sbl else None
    if sbl and sbl_hit and sbl_hit[0] != re.sub(r'\s+', '', sbl):
        log(f"[..] SBL repaired from county data: '{sbl}' → '{sbl_hit[0]}'")
        sbl = sbl_hit[0]

    if sbl and not sbl_hit:
        if addr_hit:
            log(f"[!]  SBL '{sbl}' not in county data — using {addr_hit[0]} (from address) instead")
            sbl = addr_hit[0]
            sources["sbl"] = "parcel"
        else:
            log(f"[!]  SBL '{sbl}' not found in county parcel data — verify")
    elif sbl and sbl_hit and addr_hit and sbl_hit[0] != addr_hit[0]:
        log(f"[!]  SBL/address conflict: form says {sbl_hit[0]}, county lists "
            f"{num} {street} as {addr_hit[0]} — verify before filing")

    if not sbl and addr_hit:
        sbl = addr_hit[0]
        sources["sbl"] = "parcel"
        log(f"[OK] SBL filled from county parcel data: {sbl}")

    if not street and sbl_hit and sbl_hit[1]:
        m = re.match(r'^(\d+)\s+(.+)$', sbl_hit[1])
        if m:
            num, street = m.group(1), m.group(2)
            sources["address"] = "parcel"
            log(f"[OK] Address filled from county parcel data: {num} {street}")

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

def extract_fields_with_claude(page_png_bytes, api_key, app_no="", model="claude-haiku-4-5-20251001"):
    import anthropic, base64
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
    response = client.messages.create(
        model=model,
        max_tokens=150,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
    data = json.loads(raw)
    permit_id = str(data.get("permit_id", "")).strip()
    address   = str(data.get("address",   "")).strip()
    section   = str(data.get("section",   "")).strip()
    block     = str(data.get("block",     "")).strip()
    lot       = str(data.get("lot",       "")).strip()
    sbl = f"{section}-{block}-{lot}" if (section and block and lot) else ""
    return permit_id, address, sbl


# ── History helpers ────────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []


def append_history(permit_id, address, sbl, status="OPEN"):
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
    return con


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
            return con.execute(f"""
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


# ── File watcher ───────────────────────────────────────────────────────────────

class ScanHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=self._wait_and_notify, args=(event.src_path,), daemon=True).start()

    def _wait_and_notify(self, path):
        prev = -1
        for _ in range(40):
            try:
                size = os.path.getsize(path)
            except OSError:
                time.sleep(0.5)
                continue
            if size == prev and size > 0:
                break
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
        self.status_var  = tk.StringVar(value="OPEN")
        self.last_ocr_text = ""

        # Each entry: {"current": path_in_staging, "renamed": False}
        self.staged = []
        self._scanning = False
        self._src_labels = {}
        self._current_sources = {"permit": "", "address": "", "sbl": ""}

        # Per-folder active toggles
        self.folder_active = {folder: tk.BooleanVar(value=True) for folder in SCAN_FOLDERS}

        os.makedirs(STAGING_FOLDER, exist_ok=True)

        self._build_ui()
        self._start_watchers()
        self._recover_staging()

        for v in (self.permit_id, self.street_num, self.street_name):
            v.trace_add("write", lambda *_: self._refresh_path())
        self.sbl.trace_add("write", lambda *_: self._validate_sbl())
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

        sf = ttk.Frame(info)
        sf.grid(row=4, column=0, columnspan=3, pady=(10, 2))
        ttk.Radiobutton(sf, text="OPEN",   variable=self.status_var, value="OPEN").pack(side="left", padx=20)
        ttk.Radiobutton(sf, text="CLOSED", variable=self.status_var, value="CLOSED").pack(side="left", padx=20)

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
        for i, folder in enumerate(SCAN_FOLDERS):
            label = os.path.basename(folder) or folder
            var   = self.folder_active[folder]
            row_f = ttk.Frame(wf)
            row_f.grid(row=i, column=0, sticky="ew", pady=2)
            ttk.Label(row_f, text=folder, font=("Consolas", 8), foreground="gray").pack(side="left")
            btn = tk.Button(row_f, textvariable=tk.StringVar(),
                            width=6, relief="groove", cursor="hand2",
                            command=lambda f=folder: self._toggle_watcher(f))
            btn.pack(side="right", padx=(8, 0))
            self.folder_active[folder].trace_add("write", lambda *_, f=folder: self._update_toggle_btn(f))
            btn._folder = folder
            if not hasattr(self, "_toggle_btns"):
                self._toggle_btns = {}
            self._toggle_btns[folder] = btn
            self._update_toggle_btn(folder)

        lf = ttk.LabelFrame(self, text="File Activity", padding=10)
        lf.grid(row=3, column=0, **p, sticky="ew")
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
        self._preview_photo = None

        prog_row = ttk.Frame(lf)
        prog_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_row, variable=self.progress_var,
                                            maximum=100, mode="determinate", length=260)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_pct = ttk.Label(prog_row, text="", font=("Consolas", 8), width=5, anchor="e")
        self.progress_pct.pack(side="left", padx=(6, 0))

        bf = ttk.Frame(self)
        bf.grid(row=4, column=0, **p)
        self.confirm_btn = ttk.Button(bf, text="Confirm & Rename", command=self._confirm_rename,
                                      width=18, state="disabled")
        self.confirm_btn.pack(side="left", padx=6)
        ttk.Button(bf, text="New Permit",     command=self._new_permit,    width=12).pack(side="left", padx=6)
        ttk.Button(bf, text="Open Staging",   command=self._open_staging,  width=13).pack(side="left", padx=6)
        ttk.Button(bf, text="History",        command=self._show_history,  width=9).pack(side="left", padx=6)
        ttk.Button(bf, text="Search",         command=self._show_archive,  width=8).pack(side="left", padx=6)
        ttk.Button(bf, text="Show OCR",       command=self._show_ocr,      width=9).pack(side="left", padx=6)
        ttk.Button(bf, text="Re-OCR",         command=self._reocr_staging, width=8).pack(side="left", padx=6)
        ttk.Button(bf, text="API Key",        command=self._set_api_key,   width=8).pack(side="left", padx=6)

        self._stats_var = tk.StringVar()
        ttk.Label(self, textvariable=self._stats_var,
                  font=("Segoe UI", 8), foreground="#888888").grid(
                  row=5, column=0, pady=(0, 6))

    # ── Path helpers ──────────────────────────────────────────────────────────

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

    def _laserfiche_path(self):
        num = self.street_num.get().strip()
        st  = self.street_name.get().strip().upper().rstrip('.')
        if not num or not st:
            return ""
        st_p = st + "."   # Laserfiche convention: "CROW HILL RD."
        return rf"TownOfYorktown\Building Department\Parcels\{st[0]}\{st_p}\{num} {st_p}"

    def _refresh_path(self):
        path = self._laserfiche_path()
        if not path:
            self.path_label.config(text="Fill in street info above", foreground="gray")
        else:
            self.path_label.config(text=path, foreground="#0055cc")

    def _copy_path(self):
        path = self._laserfiche_path()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)
            self._log("[OK] Path copied to clipboard")

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

    def _update_preview(self, png_bytes):
        from PIL import Image, ImageTk
        import io
        try:
            img = Image.open(io.BytesIO(png_bytes))
            img.thumbnail((115, 155), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo  # hold reference so GC doesn't collect it
            self._preview_label.config(image=photo, text="")
        except Exception:
            pass

    def _clear_preview(self):
        self._preview_photo = None
        self._preview_label.config(image="", text="no\npreview")

    # ── File handling ─────────────────────────────────────────────────────────

    def _on_file(self, path):
        self.after(0, self._handle_file, path)

    def _handle_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in SKIP_EXTENSIONS:
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
        self._log(f"[IN]  {filename}  →  staging (copy)")

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
        """Run full extraction on a PDF. Returns (permit, num, street, sbl). Safe to call from any thread."""
        import fitz
        doc = fitz.open(path)
        num_pages = min(3, len(doc))

        self.after(0, self._set_progress, 5)

        # Pass 1: native text page detection (instant for digital PDFs)
        official_idx = None
        application_idx = None
        for i in range(num_pages):
            ptype = detect_permit_type(doc[i].get_text())
            if ptype == "official" and official_idx is None:
                official_idx = i
            elif ptype == "application" and application_idx is None:
                application_idx = i

        self.after(0, self._set_progress, 20)

        # Pass 2: if official permit not yet found, OCR-scan pages to look for it.
        # Runs even when application was found in Pass 1 — official always wins.
        _ocr_pct = [40, 60, 75]
        if official_idx is None:
            self.after(0, self._log, "[..] No official permit in native text — scanning pages with OCR...")
            for i in range(num_pages):
                ptype = detect_permit_type(extract_text_from_page(doc, i))
                self.after(0, self._set_progress, _ocr_pct[i])
                if ptype == "official":
                    official_idx = i
                    break
                if ptype == "application" and application_idx is None:
                    application_idx = i

        if official_idx is not None:
            target, permit_type = official_idx, "official"
        elif application_idx is not None:
            # Application forms are staged for filing but not used as a data source
            self.after(0, self._log, "[--] Application form detected — staged for filing, not used as data source")
            self._archive_scan_text(doc, path, "", "application")
            doc.close()
            return "", "", "", "", {"permit": "", "address": "", "sbl": "", "form_type": "application", "form_page": application_idx + 1}
        else:
            target, permit_type = 0, "unknown"

        self.after(0, self._log, f"[..] Form type: {permit_type} (page {target + 1})")

        # Push a quick 1.5× render of the target page to the preview panel
        try:
            import fitz as _fitz
            _prev = doc[target].get_pixmap(matrix=_fitz.Matrix(1.5, 1.5))
            self.after(0, self._update_preview, _prev.tobytes("png"))
            del _prev
        except Exception:
            pass

        native_text = doc[target].get_text()
        if len(native_text.strip()) > 80:
            text = native_text
        else:
            text = extract_text_from_page(doc, target)
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

        # Claude fills missing fields; for the orange folder (unknown) it also overrides tesseract_hw
        handwritten = permit_type == "unknown"
        if not (permit and address and sbl) or handwritten:
            api_key = load_claude_key()
            if api_key:
                try:
                    missing = [n for n, v in [("permit", permit), ("address", address), ("sbl", sbl)] if not v]
                    self.after(0, self._set_progress, 85)
                    self.after(0, self._log, f"[..] Sending to Claude (missing: {', '.join(missing)})...")
                    # Handwritten forms need higher zoom + a stronger model to read reliably
                    zoom  = 3.0 if handwritten else 2.0
                    cl_model = "claude-sonnet-4-6" if handwritten else "claude-haiku-4-5-20251001"
                    pix = doc[target].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                    cl_permit, cl_address, cl_sbl = extract_fields_with_claude(
                        pix.tobytes("png"), api_key, app_no=app_no, model=cl_model)
                    self.after(0, self._log, f"[..] Claude returned permit='{cl_permit}' address='{cl_address}' sbl='{cl_sbl}'")
                    cl_permit_digits = re.sub(r'\D', '', cl_permit)
                    permit_slot_open = not permit or (handwritten and sources["permit"] == "tesseract_hw")
                    if permit_slot_open and cl_permit and '-' not in cl_permit and len(cl_permit_digits) == 8:
                        if app_no_digits and cl_permit_digits == app_no_digits:
                            self.after(0, self._log, f"[!]  Claude returned application number ({cl_permit}) — discarding")
                        else:
                            permit = cl_permit
                            sources["permit"] = "claude"
                    addr_slot_open = not address or (handwritten and sources["address"] == "tesseract_hw")
                    if addr_slot_open and cl_address:
                        cl_address = re.sub(r',?\s*(Yorktown|New York|NY|\d{5}).*$', '', cl_address, flags=re.IGNORECASE).strip()
                        suffix_m = re.search(rf'\b({STREET_SUFFIXES})\b\.?', cl_address, re.IGNORECASE)
                        if suffix_m:
                            cl_address = cl_address[:suffix_m.end()].strip().rstrip(',.')
                        address = cl_address
                        sources["address"] = "claude"
                    sbl_slot_open = not sbl or (handwritten and sources["sbl"] == "tesseract_hw")
                    if sbl_slot_open and cl_sbl:
                        sbl = cl_sbl
                        sources["sbl"] = "claude"
                except Exception as e:
                    self.after(0, self._log, f"[!]  Claude error: {e}")

        doc.close()
        num, street = split_address(address) if address else ("", "")
        if street:
            corrected = fuzzy_match_street(street)
            if corrected != street:
                self.after(0, self._log, f"[..] Street corrected: '{street}' → '{corrected}'")
            street = corrected
        num, street, sbl, sources = reconcile_with_parcels(
            num, street, sbl, sources, lambda m: self.after(0, self._log, m))
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

    def _apply_extracted(self, permit, num, street, sbl, sources=None):
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
        elif not self.permit_id.get().strip():
            self._log("[!]  Permit ID not found — enter manually")

        if address_wins:
            self.street_num.set(num)
            self.street_name.set(street)
            self._current_sources["address"] = sources.get("address", "")
            self._update_src_label("address", sources.get("address", ""))
            if num:
                self._log(f"[OK] Address: {num} {street}")
            else:
                self._log(f"[?]  Address found (check number): {street}")
        elif not self.street_name.get().strip():
            self._log("[!]  Address not found — enter manually")

        self._copy_path()

        if sbl_wins:
            self.sbl.set(sbl)
            self._current_sources["sbl"] = sources.get("sbl", "")
            self._update_src_label("sbl", sources.get("sbl", ""))
            self._log(f"[OK] SBL: {sbl}  (click Copy when ready)")
        elif not self.sbl.get().strip():
            self._log("[!]  SBL not found — enter manually")

        self.confirm_btn.config(state="normal")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _confirm_rename(self):
        permit = self.permit_id.get().strip()
        status = self.status_var.get()
        address = f"{self.street_num.get().strip()} {self.street_name.get().strip().upper()}".strip()
        sbl = self.sbl.get().strip()

        if not permit:
            self._log("[!]  Enter a Permit ID before confirming")
            return

        # Duplicate detection — warn if this permit ID appears in history
        prior = [e for e in load_history() if e.get("permit_id", "").startswith(permit[:8])]
        if prior:
            last = prior[0]
            if not messagebox.askyesno(
                    "Duplicate Permit",
                    f"Permit {permit} was already filed on {last['date']}.\n"
                    f"Address on file: {last.get('address', '—')}\n\nFile it again?"):
                return

        unrenamed = [e for e in self.staged if not e["renamed"]]
        sorted_entries = sorted(unrenamed,
                                key=lambda e: os.path.getctime(e["current"]) if os.path.exists(e["current"]) else 0,
                                reverse=True)

        renamed_pairs = []
        for i, entry in enumerate(sorted_entries):
            old_path = entry["current"]
            ext = os.path.splitext(old_path)[1].lower()
            new_name = f"{permit} {status}{ext}" if i == 0 else f"{permit} - {i + 1}{ext}"
            new_path = os.path.join(STAGING_FOLDER, new_name)
            try:
                os.rename(old_path, new_path)
                entry["current"] = new_path
                entry["renamed"] = True
                renamed_pairs.append((os.path.basename(old_path), new_name))
                self._log(f"[OK] {new_name}")
            except Exception as e:
                self._log(f"[X]  {os.path.basename(old_path)}: {e}")

        # Touch the main file last so it has the newest mtime — Laserfiche picks
        # the newest file as the document name when multiple files are dragged in.
        if sorted_entries and sorted_entries[0]["renamed"]:
            try:
                os.utime(sorted_entries[0]["current"], None)
            except Exception:
                pass

        append_history(permit, address, sbl, status)
        try:
            archive_confirm(renamed_pairs, permit, address, sbl, status, self._laserfiche_path())
        except Exception as e:
            self._log(f"[!]  Archive update failed: {e}")
        self._update_stats()
        self.confirm_btn.config(state="disabled")
        self._log(f"[--] {len(sorted_entries)} file(s) renamed — drag from staging to Laserfiche")

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
        self._clear_preview()
        self.permit_id.set("")
        self.street_num.set("")
        self.street_name.set("")
        self.sbl.set("")
        self.status_var.set("OPEN")
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
            self.permit_id.set(vals[1])
            self.street_num.set(num)
            self.street_name.set(street)
            self.sbl.set(vals[3])
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
            if rec["permit_id"]:
                self.permit_id.set(rec["permit_id"])
            num, street = split_address(rec["address"]) if rec["address"] else ("", "")
            if street:
                self.street_num.set(num)
                self.street_name.set(street)
            if rec["sbl"]:
                self.sbl.set(rec["sbl"])
            if rec["status"]:
                self.status_var.set(rec["status"])
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
        pdfs = [os.path.join(STAGING_FOLDER, f) for f in os.listdir(STAGING_FOLDER)
                if f.lower().endswith(".pdf")]
        for p in pdfs:
            if not any(e["current"] == p for e in self.staged):
                self.staged.append({"current": p, "renamed": False})
                self._log(f"[..] Recovered: {os.path.basename(p)}")
        if pdfs:
            self.confirm_btn.config(state="normal")
            self._log(f"[--] {len(pdfs)} file(s) found in staging from previous session")

    def _reocr_staging(self):
        if self._scanning:
            self._log("[--] Scan already in progress — please wait")
            return
        pdfs = sorted(
            [os.path.join(STAGING_FOLDER, f) for f in os.listdir(STAGING_FOLDER)
             if f.lower().endswith(".pdf")],
            key=os.path.getctime  # oldest first — first file to arrive in staging is tried first
        )
        if not pdfs:
            self._log("[!]  No PDFs in staging folder")
            return
        # Register any untracked files (drag-dropped, not from watcher)
        for p in pdfs:
            if not any(e["current"] == p for e in self.staged):
                self.staged.append({"current": p, "renamed": False})
                self._log(f"[..] Registered: {os.path.basename(p)}")
        self._scanning = True
        self._set_progress(0)
        self._log(f"[..] Re-OCR: classifying and scanning {len(pdfs)} file(s)...")
        def _run_reocr():
            try:
                self._reocr_scan_loop(pdfs)
            finally:
                self._scanning = False
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
            from PIL import Image
            doc = fitz.open(path)
            num_pages = min(3, len(doc))
            fallback = "unknown"
            # Native text pass — instant for digital PDFs
            for i in range(num_pages):
                pt = detect_permit_type(doc[i].get_text())
                if pt == "official":
                    doc.close()
                    return "official"
                elif pt == "application":
                    fallback = "application"
            if fallback == "application":
                doc.close()
                return "application"
            # Quick single-pass OCR — cheaper than full dual-pass used during extraction
            for i in range(num_pages):
                pix  = doc[i].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                gray = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")
                try:
                    text = pytesseract.image_to_string(gray, config="--psm 3 --oem 3")
                    pt = detect_permit_type(text)
                    if pt == "official":
                        doc.close()
                        return "official"
                    elif pt == "application":
                        fallback = "application"
                except Exception:
                    pass
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
            ft = self._classify_file(path)
            buckets[ft].append(path)
            self.after(0, self._log, f"[..] {os.path.basename(path)} → {_TIER_LABEL[ft]}")

        # Application forms are staged for filing but not used as a data source
        for path in buckets["application"]:
            self.after(0, self._log, f"[--] {os.path.basename(path)} — application form, skipped as data source")

        # ── Phase 2: Extract in tier order — official first, orange folder fills gaps ──
        best_permit = "";  best_permit_rank = 0
        best_num    = "";  best_street = "";  best_addr_rank = 0
        best_sbl    = "";  best_sbl_rank  = 0
        best_form_type = "";  best_form_page = 0
        best_sources = {"permit": "", "address": "", "sbl": ""}

        for tier in ("official", "unknown"):
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
        handler = ScanHandler(self._on_file)
        self.observer = Observer()
        for folder in SCAN_FOLDERS:
            if os.path.exists(folder):
                self.observer.schedule(handler, folder, recursive=False)
                self._log(f"[--] Watching {folder}")
            else:
                self._log(f"[!]  Not found: {folder}")
        self.observer.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.observer.stop()
        self.observer.join()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
