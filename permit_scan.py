import tkinter as tk
from tkinter import ttk, messagebox
import os
import threading
import time
import re
import json
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import shutil
import subprocess

SCAN_FOLDERS   = [r"U:\Documents\Scans", r"F:\scan"]
STAGING_FOLDER = r"C:\Users\nkhoury\Documents\Permit Staging"
HISTORY_FILE   = r"C:\Users\nkhoury\permit_scan_history.json"
CONFIG_FILE    = r"C:\Users\nkhoury\permit_scan_config.json"
SKIP_EXTENSIONS   = {".tmp", ".part", ".crdownload", ""}
TESSERACT_PATH    = r"C:\Users\nkhoury\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
STREET_LIST_FILE  = r"C:\Users\nkhoury\yorktown_streets.txt"

IGNORE_ADDRESSES = ["363 UNDERHILL AVE"]

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
    hits = difflib.get_close_matches(street_name.upper(), KNOWN_STREETS, n=1, cutoff=0.8)
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
    r"location\s+of\s+(?:construction|building|work)",
    r"present\s+address\s+of\s+owner",
    r"address\s+of\s+owner",
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


def extract_native_text(filepath):
    import fitz
    doc = fitz.open(filepath)
    text = "\n".join(doc[i].get_text() for i in range(min(3, len(doc))))
    doc.close()
    return text


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

    ocr = ocr_a if len(ocr_a) >= len(ocr_b) else ocr_b
    return (native + "\n" + ocr).strip()


def preprocess_for_ocr(img):
    from PIL import ImageEnhance, ImageFilter
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_image(img, pytesseract):
    cfg3  = "--oem 1 --psm 3"   # LSTM + auto layout detection (good for forms)
    cfg6  = "--oem 1 --psm 6"   # LSTM + uniform block
    cfg11 = "--oem 1 --psm 11"  # LSTM + sparse/handwritten layout
    results = [pytesseract.image_to_string(img, config=c) for c in [cfg3, cfg6, cfg11]]
    # Score by clean printable ASCII — raw length is inflated by OCR garbage symbols
    def _score(t):
        return sum(1 for c in t if c.isascii() and (c.isalnum() or c in ' \n.,:-#/'))
    return max(results, key=_score)


def extract_text_from_pdf(filepath):
    import fitz
    import pytesseract
    from PIL import Image
    import io

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    doc = fitz.open(filepath)
    full_text = ""
    for page_num in range(min(3, len(doc))):
        page = doc[page_num]
        native = page.get_text().strip()
        mat = fitz.Matrix(3.0, 3.0)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img = preprocess_for_ocr(img)
        ocr = ocr_image(img, pytesseract)
        # Always combine both — native text has printed labels,
        # OCR captures the handwritten values; we need both
        full_text += native + "\n" + ocr + "\n"
    doc.close()
    return full_text


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
    sec_m = re.search(r'\bsection\b[^\d\n]{0,25}(\d{1,3}(?:[.,\- ]\d{1,3})?)', text, re.IGNORECASE)
    blk_m = re.search(r'(?:bl?|l)ock[^\d\n]{0,20}(\d{1,3})',                    text, re.IGNORECASE)
    lot_m = re.search(r'lot[^\d\n]{0,20}(\d{1,3})',                              text, re.IGNORECASE)

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

def extract_fields_with_claude(page_png_bytes, api_key, app_no=""):
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
        "- permit_id: ONLY the value from the field labeled 'Permit No.', 'Permit #', or 'Building Permit No.' "
        + app_no_hint +
        "If the Permit No. field is blank or absent, return empty string. "
        "Permit IDs are 8 digits, sometimes followed by a type suffix with no space (e.g. '20100027DEMO'). "
        "Return the digits and suffix together, no hyphens — e.g. '20100240' or '20100027DEMO'\n"
        "- address: the job site address — where the construction work is being performed. "
        "On printed Building Permits this is labeled 'Location:' and appears mid-form after the permit number. "
        "On application forms it may be labeled 'Location of Work', 'Job Address', or 'Site Address'. "
        "IMPORTANT: the form header always starts with 'Town of Yorktown / 363 Underhill Avenue / Yorktown Heights' — "
        "that is the Building Department's address, NOT the job site. Never return '363 Underhill Avenue'. "
        "Also do not return the owner's mailing address even if it appears nearby.\n"
        "- section: number from the SECTION or SBL SECTION field\n"
        "- block: number from the BLOCK field\n"
        "- lot: number from the LOT or LOT(S) field\n\n"
        "Return ONLY a JSON object, nothing else. Use empty string for anything unclear.\n"
        'Example: {"permit_id":"20100240","address":"1005 East Main St","section":"16.10","block":"4","lot":"25"}'
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
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


def append_history(permit_id, address, sbl):
    history = load_history()
    history.insert(0, {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "permit_id": permit_id,
        "address": address,
        "sbl": sbl,
    })
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[:1000], f, indent=2)


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

        # Per-folder active toggles
        self.folder_active = {folder: tk.BooleanVar(value=True) for folder in SCAN_FOLDERS}

        os.makedirs(STAGING_FOLDER, exist_ok=True)

        self._build_ui()
        self._start_watchers()

        for v in (self.permit_id, self.street_num, self.street_name):
            v.trace_add("write", lambda *_: self._refresh_path())

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        p = {"padx": 12, "pady": 6}

        info = ttk.LabelFrame(self, text="Permit Info", padding=10)
        info.grid(row=0, column=0, **p, sticky="ew")

        ttk.Label(info, text="Permit ID").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.permit_id, width=30).grid(row=0, column=1, padx=8, pady=2)

        ttk.Label(info, text="Street Number").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.street_num, width=10).grid(row=1, column=1, padx=8, pady=2, sticky="w")

        ttk.Label(info, text="Street Name").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(info, textvariable=self.street_name, width=30).grid(row=2, column=1, padx=8, pady=2)

        ttk.Label(info, text="SBL").grid(row=3, column=0, sticky="w", pady=2)
        sbl_frame = ttk.Frame(info)
        sbl_frame.grid(row=3, column=1, padx=8, pady=2, sticky="w")
        ttk.Entry(sbl_frame, textvariable=self.sbl, width=18).pack(side="left")
        ttk.Button(sbl_frame, text="Copy", width=5, command=self._copy_sbl).pack(side="left", padx=(4, 0))

        sf = ttk.Frame(info)
        sf.grid(row=4, column=0, columnspan=2, pady=(10, 2))
        ttk.Radiobutton(sf, text="OPEN",   variable=self.status_var, value="OPEN").pack(side="left", padx=20)
        ttk.Radiobutton(sf, text="CLOSED", variable=self.status_var, value="CLOSED").pack(side="left", padx=20)

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
        self.log_box = tk.Text(lf, height=8, width=50, state="disabled",
                               font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                               relief="flat", cursor="arrow")
        self.log_box.grid()

        bf = ttk.Frame(self)
        bf.grid(row=4, column=0, **p)
        self.confirm_btn = ttk.Button(bf, text="Confirm & Rename", command=self._confirm_rename,
                                      width=18, state="disabled")
        self.confirm_btn.pack(side="left", padx=6)
        ttk.Button(bf, text="New Permit",     command=self._new_permit,    width=12).pack(side="left", padx=6)
        ttk.Button(bf, text="Open Staging",   command=self._open_staging,  width=13).pack(side="left", padx=6)
        ttk.Button(bf, text="History",        command=self._show_history,  width=9).pack(side="left", padx=6)
        ttk.Button(bf, text="Show OCR",       command=self._show_ocr,      width=9).pack(side="left", padx=6)
        ttk.Button(bf, text="Re-OCR",         command=self._reocr_staging, width=8).pack(side="left", padx=6)
        ttk.Button(bf, text="API Key",        command=self._set_api_key,   width=8).pack(side="left", padx=6)

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

    def _extract_fields(self, path):
        """Run full extraction on a PDF. Returns (permit, num, street, sbl). Safe to call from any thread."""
        import fitz
        doc = fitz.open(path)
        num_pages = min(6, len(doc))

        # Pass 1: native text page detection (instant for digital PDFs)
        official_idx = None
        application_idx = None
        for i in range(num_pages):
            ptype = detect_permit_type(doc[i].get_text())
            if ptype == "official" and official_idx is None:
                official_idx = i
            elif ptype == "application" and application_idx is None:
                application_idx = i

        # Pass 2: if official permit not yet found, OCR-scan pages to look for it.
        # Runs even when application was found in Pass 1 — official always wins.
        if official_idx is None:
            self.after(0, self._log, "[..] No official permit in native text — scanning pages with OCR...")
            for i in range(num_pages):
                ptype = detect_permit_type(extract_text_from_page(doc, i))
                if ptype == "official":
                    official_idx = i
                    break
                if ptype == "application" and application_idx is None:
                    application_idx = i

        if official_idx is not None:
            target, permit_type = official_idx, "official"
        elif application_idx is not None:
            target, permit_type = application_idx, "application"
        else:
            target, permit_type = 0, "unknown"

        self.after(0, self._log, f"[..] Form type: {permit_type} (page {target + 1})")

        native_text = doc[target].get_text()
        if len(native_text.strip()) > 80:
            text = native_text
        else:
            text = extract_text_from_page(doc, target)
        self.last_ocr_text = text

        self.after(0, self._log, f"[..] Text sample: {text[:120].strip()!r}")

        app_no        = find_application_number(text)
        app_no_digits = re.sub(r'\D', '', app_no)
        if app_no:
            self.after(0, self._log, f"[..] Application No. detected: {app_no} — will not use as permit ID")

        permit  = find_permit_number(text, blocked_digits=app_no_digits or None)
        address = find_address(text)
        sbl     = find_sbl(text)

        # Claude fills whatever text extraction missed
        if not (permit and address and sbl):
            api_key = load_claude_key()
            if api_key:
                try:
                    missing = [n for n, v in [("permit", permit), ("address", address), ("sbl", sbl)] if not v]
                    self.after(0, self._log, f"[..] Sending to Claude (missing: {', '.join(missing)})...")
                    pix = doc[target].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    cl_permit, cl_address, cl_sbl = extract_fields_with_claude(pix.tobytes("png"), api_key, app_no=app_no)
                    self.after(0, self._log, f"[..] Claude returned permit='{cl_permit}' address='{cl_address}' sbl='{cl_sbl}'")
                    cl_permit_digits = re.sub(r'\D', '', cl_permit)
                    if not permit and cl_permit and '-' not in cl_permit and len(cl_permit_digits) == 8:
                        if app_no_digits and cl_permit_digits == app_no_digits:
                            self.after(0, self._log, f"[!]  Claude returned application number ({cl_permit}) — discarding")
                        else:
                            permit = cl_permit
                    if not address and cl_address:
                        cl_address = re.sub(r',?\s*(Yorktown|New York|NY|\d{5}).*$', '', cl_address, flags=re.IGNORECASE).strip()
                        suffix_m = re.search(rf'\b({STREET_SUFFIXES})\b\.?', cl_address, re.IGNORECASE)
                        if suffix_m:
                            cl_address = cl_address[:suffix_m.end()].strip().rstrip(',.')
                        address = cl_address
                    if not sbl and cl_sbl:
                        sbl = cl_sbl
                except Exception as e:
                    self.after(0, self._log, f"[!]  Claude error: {e}")

        doc.close()
        num, street = split_address(address) if address else ("", "")
        if street:
            corrected = fuzzy_match_street(street)
            if corrected != street:
                self.after(0, self._log, f"[..] Street corrected: '{street}' → '{corrected}'")
            street = corrected
        return permit, num, street, sbl

    def _ocr_and_fill(self, path):
        try:
            permit, num, street, sbl = self._extract_fields(path)
            self.after(0, self._apply_extracted, permit, num, street, sbl)
        except Exception as e:
            self.after(0, self._log, f"[!]  OCR error: {e}")
            self.after(0, lambda: self.confirm_btn.config(state="normal"))

    def _apply_extracted(self, permit, num, street, sbl):
        if permit:
            self.permit_id.set(permit)
            self._log(f"[OK] Permit ID: {permit}")
        else:
            self._log("[!]  Permit ID not found — enter manually")

        if num and street:
            self.street_num.set(num)
            self.street_name.set(street)
            self._log(f"[OK] Address: {num} {street}")
        elif street:
            self.street_name.set(street)
            self._log(f"[?]  Address found (check number): {street}")
        else:
            self._log("[!]  Address not found — enter manually")

        self._copy_path()

        if sbl:
            self.sbl.set(sbl)
            self._log(f"[OK] SBL: {sbl}  (click Copy when ready)")
        else:
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

        unrenamed = [e for e in self.staged if not e["renamed"]]
        sorted_entries = sorted(unrenamed,
                                key=lambda e: os.path.getmtime(e["current"]) if os.path.exists(e["current"]) else 0,
                                reverse=True)

        for i, entry in enumerate(sorted_entries):
            old_path = entry["current"]
            ext = os.path.splitext(old_path)[1].lower()
            new_name = f"{permit} {status}{ext}" if i == 0 else f"{permit} - {i + 1}{ext}"
            new_path = os.path.join(STAGING_FOLDER, new_name)
            try:
                os.rename(old_path, new_path)
                entry["current"] = new_path
                entry["renamed"] = True
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

        append_history(permit, address, sbl)
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

        cols    = ("date", "permit_id", "address", "sbl")
        headers = ("Date", "Permit ID", "Address", "SBL")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=20)
        for col, hdr in zip(cols, headers):
            tree.heading(col, text=hdr)
        tree.column("date",      width=120)
        tree.column("permit_id", width=110)
        tree.column("address",   width=200)
        tree.column("sbl",       width=100)

        for entry in history:
            tree.insert("", "end", values=(
                entry.get("date", ""), entry.get("permit_id", ""),
                entry.get("address", ""), entry.get("sbl", ""),
            ))

        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        sb.grid(row=0, column=1, sticky="ns", pady=8)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        if not history:
            tree.insert("", "end", values=("No history yet", "", "", ""))

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
        self._log(f"[..] Re-OCR: scanning {len(pdfs)} file(s), oldest first...")
        threading.Thread(target=self._reocr_scan_loop, args=(pdfs,), daemon=True).start()

    def _reocr_scan_loop(self, pdfs):
        best_permit = ""
        best_num    = ""
        best_street = ""
        best_sbl    = ""

        for path in pdfs:
            self.after(0, self._log, f"[..] Trying: {os.path.basename(path)}")
            try:
                permit, num, street, sbl = self._extract_fields(path)
                updates = []
                if permit and not best_permit:
                    best_permit = permit
                    updates.append(f"permit={permit}")
                # num and street are always updated as a pair — never mix from different files.
                # Prefer the address with the longer street number (more digits = more complete).
                if street and (not best_street or len(num) > len(best_num)):
                    best_num    = num
                    best_street = street
                    updates.append(f"address={num} {street}")
                if sbl and not best_sbl:
                    best_sbl = sbl
                    updates.append(f"sbl={sbl}")
                if updates:
                    self.after(0, self._log, f"[..] Got from {os.path.basename(path)}: {', '.join(updates)}")
                else:
                    self.after(0, self._log, f"[--] Nothing new in {os.path.basename(path)}")
            except Exception as e:
                self.after(0, self._log, f"[!]  Error on {os.path.basename(path)}: {e}")

        if best_permit or best_street or best_sbl:
            self.after(0, self._apply_extracted, best_permit, best_num, best_street, best_sbl)
        else:
            self.after(0, self._log, "[!]  No data found in any staging file — fill manually")
            self.after(0, lambda: self.confirm_btn.config(state="normal"))
        self._scanning = False

    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        with open(r"C:\Users\nkhoury\permit_scan_debug.log", "a", encoding="utf-8") as _f:
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
