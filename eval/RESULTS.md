# Extraction Quality — Evaluation Results

*Generated 2026-08-10T10:56:22 — see `evaluate.py` for methodology.*

Ground truth: **14 human-confirmed permit batches** (36 scan files re-processed, 23.7s avg/file). Confirmed filings are the reference: a staff member reviewed every value before filing, correcting any the pipeline got wrong.

## Accuracy by pipeline stage

Each stage adds one tier of the cascade. *Coverage* = share of batches where the stage produced a value; *accuracy* = share of batches where the value matched the confirmed one (addresses suffix-normalized).

| Stage | Permit ID | Address | SBL | All 3 correct |
|---|---|---|---|---|
| Native PDF text | 0% (cov 0%) | 0% (cov 0%) | 0% (cov 0%) | 0% |
| + Tesseract OCR | 100% (cov 100%) | 86% (cov 93%) | 64% (cov 93%) | 57% |
| + AI vision | 100% (cov 100%) | 93% (cov 100%) | 71% (cov 100%) | 64% |
| Full pipeline (+ county verify) | 100% (cov 100%) | 100% (cov 100%) | 86% (cov 100%) | 86% |

## Page classification — missed-forms estimate

*The AI labeling pass covered only the pages the local classifier abstained on, so this is a missed-forms estimate, not a confusion matrix: pages in the unclassified tail that the AI called a form are candidate local misses (upper bound — AI labels are keyword-mapped and themselves imperfect).*

Local classifier identified 4,081 of 10,817 census pages (38%); the remainder was AI-cataloged.

| Form class | Found locally | Candidate misses in tail | Est. recall |
|---|---|---|---|
| official | 912 | 27 | 97.1% |
| application | 539 | 217 | 71.3% |
| orange_cover | 732 | 0 | 100.0% |
| inspection_sheet | 199 | 0 | 100.0% |
| plans | 1448 | 278 | 83.9% |

*Eval run cost: 23 paid vision calls, 0 cache hits, ~$0.17 API spend.*
