# Permit Scanning Assist

A desktop tool built for the **Town of Yorktown Building Department** that automates the intake of scanned building permits into the department's document management workflow.

---

## What It Does

When a permit is scanned, the tool:

1. **Watches scan folders** (`U:\Documents\Scans`, `F:\scan`) for incoming PDFs
2. **Stages copies** to `Documents\Permit Staging` — originals are never moved or deleted
3. **OCRs each document** using a tiered pipeline (native text → Tesseract → Claude AI) to extract:
   - **Permit ID** — 8-digit permit number
   - **Property Address** — street number + street name, fuzzy-matched against a validated Yorktown street list
   - **SBL** — Section-Block-Lot parcel identifier (e.g. `48.11-1-11`)
4. **Cross-checks against county parcel data** (`yorktown_parcels.db`, all 14,407 Yorktown parcels from the NYS GIS assessment roll):
   - Fills a missing SBL from the address, or a missing address from the SBL
   - Repairs OCR-mangled SBLs (dropped decimal points, dropped zero-padding — `2710-3-34` → `27.10-3-34`)
   - Corrects wrong street suffixes when the number + base name is unambiguous (`3018 Hickory Ln` → `3018 HICKORY ST`)
   - Flags an SBL that contradicts the address for review before filing
   - Parcel-sourced values show a `county` source label; the SBL field turns orange if the value isn't a real Yorktown parcel
5. **Calculates the Laserfiche path** for the document and copies it to the clipboard automatically
6. **Renames staged files** to match Yorktown filing conventions (`{PermitID} OPEN.pdf`, `{PermitID} CLOSED.pdf`, etc.)

The staff member then drags the staged file into Laserfiche and pastes the pre-built path — no typing required.

---

## Document Types Supported

| Type | Detection | Extraction Method |
|------|-----------|-------------------|
| Official Building Permit (printed) | Header text match | Native text layer → Tesseract OCR → Claude Haiku |
| Orange Folder Cover Sheet (handwritten) | Label pattern match | Tesseract → Claude Sonnet at 3× zoom |
| Application for Building Permit | Header text match | Staged for filing only — no field extraction |

Permit subtype is detected and displayed in the UI (e.g. "Electrical Permit", "Demolition Permit") rather than a generic label.

---

## UI Features

### Main Window
- **Source labels** next to each field show how the value was found: `text` (native PDF), `ocr` (Tesseract), `ai ←verify` (Claude), `ocr?` (Tesseract on handwriting)
- **SBL validator** — the SBL field turns red if the value doesn't match the Yorktown `##.##-#-##` format
- **Live page preview** — a thumbnail of the PDF page currently being analyzed updates in real time as OCR runs
- **Scan stats** — running count of permits processed today and over the past 7 days, shown at the bottom of the window
- **Per-folder pause toggles** — ON/OFF buttons to temporarily stop watching a specific scan folder
- **Keyboard shortcuts** — `Enter` to confirm & rename, `N` for New Permit, `R` for Re-OCR (suppressed while typing in fields)

### History Window
- Full scan history with **date, permit ID, address, and SBL**
- **Live search** — filters across all columns as you type
- **Sortable columns** — click any column header to sort ascending/descending
- **Multi-select delete** — select one or more rows and delete with confirmation (`Delete` key or button)
- **Clear All** — wipes the full history with confirmation
- **Double-click or Load Selected** — loads a historical permit back into the main form (useful for rescans or corrections)
- Record count shown in the status bar, updates when filtering

### Search Archive (full-text)
- Every scan's OCR/native text is kept in `permit_scan_archive.db` and indexed with SQLite FTS5 — nothing is thrown away after extraction
- **Search window** matches against permit ID, address, SBL, *and the full scanned text* ("asbestos", "detached garage", a contractor name…) with live as-you-type results and snippets
- Records are stamped at Confirm & Rename with the final filename, OPEN/CLOSED status, and Laserfiche path — so a search result tells you exactly where the document lives
- **Copy LF Path** and **Load Into Form** buttons on each result; prior history was imported automatically on first run

### Property Lookup
- One search box accepts an **address, SBL (or SBL prefix), bare street number, street name, or owner name**
- Shows matching parcels from county data (SBL, address, owner from the assessment roll)
- Selecting a parcel lists **every scan on file** for it and shows its **Laserfiche path**
- **Use This Property** fills the main form with county-verified address + SBL — useful when filing a permit for a known property without waiting on OCR

### Safety
- **Duplicate permit warning** — before renaming, checks history for the same permit ID and prompts staff if a match is found, showing the date and address it was previously filed under

---

## Tech Stack

- **Python 3 / tkinter** — desktop GUI, runs on existing department Windows machines
- **PyMuPDF (fitz)** — PDF rendering and native text extraction
- **Tesseract OCR** — local OCR for printed and handwritten text
- **Anthropic Claude API** — AI fallback for difficult or handwritten documents
  - `claude-haiku-4-5` for printed permits (fast, low cost — $1/$5 per MTok)
  - `claude-sonnet-4-6` for handwritten orange folder sheets (higher accuracy — $3/$15 per MTok)
- **difflib** — fuzzy street name matching against `yorktown_streets.txt` (cutoff 0.9)

---

## Future Goals — Full Laserfiche Integration

The current workflow still requires a staff member to manually drag files into Laserfiche and paste the filing path. The long-term goal is to eliminate that manual step entirely by integrating directly with Laserfiche.

### Planned Integration Path

**Phase 1 — Laserfiche REST API (in progress / research)**
The Town's Laserfiche instance (`LFAPP.yorktown.local`) runs self-hosted with a Web API Server component. Once IT installs the Web API Server and provides a service account, the tool will:
- Authenticate to Laserfiche via the REST API using the service account
- Create or verify the folder path (`TownOfYorktown\Building Department\Parcels\{letter}\{street}\{address}`) automatically
- Upload the renamed PDF directly into the correct Laserfiche folder
- Set metadata fields (Permit ID, SBL, Address) on the entry at upload time

**Phase 2 — Zero-Touch Filing**
After a successful OCR extraction (all three fields found with high-confidence sources), the tool will auto-file without staff confirmation. Lower-confidence results (handwritten orange folders, Claude fallbacks) will still prompt for review before filing.

**Phase 3 — Bidirectional Sync**
- Query Laserfiche to check whether a permit folder already exists before creating a new one
- Flag duplicate permit IDs before filing
- Optionally pull existing metadata back into the UI for verification

### Why Laserfiche
Laserfiche is the Town's official records management system. Every building permit, certificate of occupancy, and inspection record ultimately lives there. Automating the upload removes the last manual step in the scanning workflow and eliminates misfiled documents caused by copy-paste errors on long folder paths.

---

## Project Status

| Feature | Status |
|---------|--------|
| Folder watching (two scan paths) | Done |
| PDF staging + file rename | Done |
| Native text extraction | Done |
| Tesseract OCR (printed) | Done |
| Claude AI fallback (printed) | Done |
| Orange folder handwriting extraction | Done |
| Permit subtype detection (Electrical, Demolition, etc.) | Done |
| Laserfiche path calculation + clipboard | Done |
| Duplicate permit warning | Done |
| Scan history with search, sort, delete | Done |
| Live page preview during OCR | Done |
| Keyboard shortcuts | Done |
| SBL format validation | Done |
| Daily / weekly scan stats | Done |
| County parcel data cross-validation (address ↔ SBL) | Done |
| Full-text archive + search of every scan | Done |
| OPEN/CLOSED status tracked in history | Done |
| Laserfiche Import Agent zero-touch filing | Pending IT (Web API alternative) |
| Laserfiche REST API upload | Planned |
| Auto-filing on high-confidence results | Planned |

---

## Repository Contents

| File | Purpose |
|------|---------|
| `permit_scan.py` | Main application — GUI, OCR pipeline, extraction logic |
| `build_parcel_db.py` | Downloads all Yorktown parcels (SBL + address) from the NYS GIS tax parcel service into `~\yorktown_parcels.db`. Re-run yearly when new assessment rolls publish. |
| `yorktown_streets.txt` | Validated list of ~500 Yorktown street names used for fuzzy matching |

---

*Built for internal use by the Town of Yorktown Building Department.*
