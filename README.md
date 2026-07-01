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
4. **Calculates the Laserfiche path** for the document and copies it to the clipboard automatically
5. **Renames staged files** to match Yorktown filing conventions (`{PermitID} OPEN.pdf`, `{PermitID} CLOSED.pdf`, etc.)

The staff member then drags the staged file into Laserfiche and pastes the pre-built path — no typing required.

---

## Document Types Supported

| Type | Detection | Extraction Method |
|------|-----------|-------------------|
| Official Building Permit (printed) | Header text match | Native text layer → Tesseract OCR → Claude Haiku |
| Orange Folder Cover Sheet (handwritten) | Label pattern match | Tesseract → Claude Sonnet at 3× zoom |
| Application for Building Permit | Header text match | Staged for filing only — no field extraction |

---

## Tech Stack

- **Python 3 / tkinter** — desktop GUI, runs on existing department Windows machines
- **PyMuPDF (fitz)** — PDF rendering and native text extraction
- **Tesseract OCR** — local OCR for printed and handwritten text
- **Anthropic Claude API** — AI fallback for difficult or handwritten documents
  - Haiku model for printed permits (fast, low cost)
  - Sonnet model for handwritten orange folder sheets (higher accuracy on cursive/print mix)
- **difflib** — fuzzy street name matching against `yorktown_streets.txt`

---

## Future Goals — Full Laserfiche Integration

The current workflow still requires a staff member to manually drag files into Laserfiche and paste the filing path. The long-term goal is to eliminate that manual step entirely by integrating directly with Laserfiche.

### Planned Integration Path

**Phase 1 — Laserfiche REST API (in progress / research)**
The Town's Laserfiche instance (`LFAPP.yorktown.local`) runs Laserfiche Cloud or self-hosted with a Web API Server. Once the API Server is confirmed available on the network, the tool will:
- Authenticate to Laserfiche via the REST API using a service account
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
| Laserfiche path calculation + clipboard | Done |
| Laserfiche REST API upload | Planned |
| Auto-filing on high-confidence results | Planned |
| Duplicate permit detection | Planned |

---

## Repository Contents

| File | Purpose |
|------|---------|
| `permit_scan.py` | Main application — GUI, OCR pipeline, extraction logic |
| `yorktown_streets.txt` | Validated list of ~500 Yorktown street names used for fuzzy matching |

---

*Built for internal use by the Town of Yorktown Building Department.*
