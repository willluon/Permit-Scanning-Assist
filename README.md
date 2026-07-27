# Permit Scanning Assist

A desktop tool built for the **Town of Yorktown Building Department** that automates the intake of scanned building permits into the department's document management workflow.

---

## What It Does

When a permit is scanned, the tool:

1. **Watches scan folders** (`U:\Documents\Scans`, `F:\scan`) for incoming PDFs
2. **Stages copies** to `Documents\Permit Staging` — originals are never moved or deleted
3. **OCRs each document** using a tiered pipeline (native text → Tesseract → AI vision fallback) to extract:
   - **Permit ID** — 8-digit permit number
   - **Property Address** — street number + street name, fuzzy-matched against a validated Yorktown street list
   - **SBL** — Section-Block-Lot parcel identifier (e.g. `48.11-1-11`)
4. **Cross-checks against county parcel data** (`yorktown_parcels.db`, all 14,407 Yorktown parcels from the NYS GIS assessment roll):
   - Fills a missing SBL from the address, or a missing address from the SBL
   - Repairs OCR-mangled SBLs (dropped decimal points, dropped zero-padding — `2710-3-34` → `27.10-3-34`)
   - Repairs truncated street reads via the SBL when the street numbers agree (`3269 STO` → `3269 STONY ST`)
   - Corrects wrong street suffixes when the number + base name is unambiguous (`3018 Hickory Ln` → `3018 HICKORY ST`)
   - Flags an SBL that contradicts the address for review before filing; logs a `Verified against county parcels` line on a clean pass
5. **Archives the full text** of every scan into a searchable full-text index — nothing is thrown away after extraction
6. **Calculates the Laserfiche path** for the document and copies it to the clipboard automatically
7. **Renames staged files** to match Yorktown filing conventions (`{PermitID} OPEN.pdf`, `{PermitID} CLOSED.pdf`, etc.)

The staff member then drags the staged file into Laserfiche and pastes the pre-built path — no typing required.

---

## Document Types Supported

| Type | Detection | Extraction Method |
|------|-----------|-------------------|
| Official Building Permit (printed) | Header text match | Native text layer → Tesseract OCR → AI vision (fast tier) |
| Orange Folder Cover Sheet (handwritten) | Label pattern match | Tesseract → AI vision (high-accuracy tier, 3× zoom) |
| Application for Building Permit | Header text match | Staged for filing and archived — no field extraction |

Permit subtype is detected and displayed in the UI (e.g. "Electrical Permit", "Demolition Permit") rather than a generic label.

---

## UI Features

### Main Window
- **Source labels** next to each field show how the value was found: `text` (native PDF), `ocr` (Tesseract), `ai ←verify` (AI vision), `ocr?` (Tesseract on handwriting), `county` (county parcel data — authoritative)
- **SBL validator** — the SBL field turns red for a malformed value and orange for a well-formed SBL that isn't a real Yorktown parcel
- **Live page preview** — a thumbnail of the PDF page currently being analyzed updates in real time as OCR runs
- **Scan stats** — running count of permits processed today and over the past 7 days, shown at the bottom of the window
- **Per-folder pause toggles** — ON/OFF buttons to temporarily stop watching a specific scan folder
- **Keyboard shortcuts** — `Enter` to confirm & rename, `N` for New Permit, `R` for Re-OCR (suppressed while typing in fields)
- Buttons are grouped into two rows: workflow (Confirm & Rename, New Permit, Re-OCR, Open Staging) and lookups/tools (History, Search, Property, Show OCR, API Key)

### History Window
- Full scan history with **date, permit ID, address, SBL, and OPEN/CLOSED status**
- **Live search** — filters across all columns as you type
- **Sortable columns** — click any column header to sort ascending/descending
- **Multi-select delete** — select one or more rows and delete with confirmation (`Delete` key or button)
- **Clear All** — wipes the full history with confirmation
- **Double-click or Load Selected** — loads a historical permit back into the main form (useful for rescans or corrections)
- Record count shown in the status bar, updates when filtering

### Search Archive (full-text)
- Every scan's OCR/native text is kept in `permit_scan_archive.db` and indexed with SQLite FTS5
- **Search window** matches against permit ID, address, SBL, *and the full scanned text* ("asbestos", "detached garage", a contractor name…) with live as-you-type results and snippets
- Records are stamped at Confirm & Rename with the final filename, OPEN/CLOSED status, and Laserfiche path — so a search result tells you exactly where the document lives
- **Copy LF Path** and **Load Into Form** buttons on each result; prior history was imported automatically on first run

### Property Lookup
- One search box accepts an **address, SBL (or SBL prefix), bare street number, street name, or owner name**
- Shows matching parcels from county data (SBL, address, owner from the assessment roll)
- Selecting a parcel lists **every scan on file** for it — including older records saved with unpadded SBL formats — and shows its **Laserfiche path**
- **Use This Property** fills the main form with county-verified address + SBL, useful when filing a permit for a known property without waiting on OCR

### Safety
- **Duplicate permit warning** — before renaming, checks history for the same permit ID and prompts staff if a match is found, showing the date and address it was previously filed under

---

## Tech Stack

- **Python 3 / tkinter** — desktop GUI, runs on existing department Windows machines
- **PyMuPDF (fitz)** — PDF rendering and native text extraction
- **Tesseract OCR** — local OCR for printed and handwritten text
- **AI vision API** — cloud fallback for difficult or handwritten documents; a fast model handles printed permits and a higher-accuracy model reads handwritten cover sheets at 3× zoom
- **SQLite** — county parcel database and FTS5 full-text scan archive, both local files with no server dependency
- **difflib** — fuzzy street name matching against `yorktown_streets.txt`

---

## Future Goals — Full Laserfiche Integration

The current workflow still requires a staff member to manually drag files into Laserfiche and paste the filing path. The long-term goal is to eliminate that manual step entirely by integrating directly with Laserfiche.

### Planned Integration Path

**Phase 0 — Laserfiche Import Agent (pending IT)**
If the Town's Laserfiche license includes Import Agent, the tool can write renamed PDFs plus metadata into a watched folder and Laserfiche files them automatically — zero-touch filing with configuration only, no server-side development.

**Phase 1 — Laserfiche REST API**
The Town's Laserfiche instance (`LFAPP.yorktown.local`) runs self-hosted. Once IT installs the Web API Server component and provides a service account, the tool will:
- Authenticate to Laserfiche via the REST API using the service account
- Create or verify the folder path (`TownOfYorktown\Building Department\Parcels\{letter}\{street}\{address}`) automatically
- Upload the renamed PDF directly into the correct Laserfiche folder
- Set metadata fields (Permit ID, SBL, Address) on the entry at upload time

**Phase 2 — Zero-Touch Filing**
After a successful extraction (all three fields found with high-confidence sources and verified against county parcels), the tool will auto-file without staff confirmation. Lower-confidence results (handwritten cover sheets, AI fallbacks) will still prompt for review before filing.

**Phase 3 — Backfile Digitization**
With automated filing in place, a batch mode will process boxes of historical paper permits: classify, extract, cross-validate, and queue everything in a review screen where staff approve or fix each record in seconds instead of minutes. This retroactively fills the archive and makes property history complete.

**Phase 4 — Bidirectional Sync**
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
| AI vision fallback (printed) | Done |
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
| Property lookup (parcels, scans on file, owner search) | Done |
| OPEN/CLOSED status tracked in history | Done |
| Laserfiche Import Agent zero-touch filing | Pending IT |
| Laserfiche REST API upload | Planned |
| Auto-filing on high-confidence results | Planned |
| Backfile digitization mode | Planned (after filing automation) |

---

## Repository Contents

| File | Purpose |
|------|---------|
| `permit_scan.py` | Main application — GUI, OCR pipeline, extraction logic, lookups |
| `build_parcel_db.py` | Downloads all Yorktown parcels (SBL, address, owner) from the NYS GIS tax parcel service into `~\yorktown_parcels.db`. Re-run yearly when new assessment rolls publish. |
| `census_scans.py` | Classifies and labels every page of the historical scan folders into `~\permit_census.db` |
| `census_report.py` | Document-type catalog report over the census database |
| `yorktown_streets.txt` | Validated list of ~500 Yorktown street names used for fuzzy matching |
| `tests/` | Regression tests — see below |

### Tests

```
python -m unittest discover -s tests -v
```

No API calls, no OCR, no GUI — they run in well under a second and cover the
pure logic: form classification, permit/SBL/address extraction, street
normalization, Laserfiche path building, the county reconcile guardrails, source
ranking, and the staging-filename rules.

The reconcile tests are the important ones. They pin behavior that has already
been wrong once in production — most notably that a misread SBL which uniquely
suffix-matches some unrelated parcel must be **left alone** rather than
"repaired" into a confidently wrong parcel. Tests marked
`test_KNOWN_LIMITATION_*` record current behavior that is accepted but not
desired; they are the ones to revisit, not to trust.

Tests requiring `~\yorktown_parcels.db` skip themselves when it isn't present.

### Local Data Files (not in repo)

| File | Purpose |
|------|---------|
| `~\yorktown_parcels.db` | County parcel data: SBL ↔ address ↔ owner for every Yorktown parcel |
| `~\permit_scan_archive.db` | Full-text archive of every scan, FTS5-indexed |
| `~\permit_scan_history.json` | Scan history (date, permit, address, SBL, status) |
| `~\permit_scan_config.json` | Local configuration |

---

*Built for internal use by the Town of Yorktown Building Department.*
