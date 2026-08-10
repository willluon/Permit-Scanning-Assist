"""Extraction-quality evaluation harness for permit_scan.py.

Ground truth: human-confirmed filings in the archive DB. Every confirmed batch
went through a staff member who looked at the extracted fields and pressed
Enter (correcting them first when wrong) — the closest thing to labeled data a
production tool can have. The harness re-runs the extraction pipeline headlessly
on the batches' original scan files and scores each pipeline stage against
those confirmed values.

Stages measured (via _stage_snapshot hooks inside _extract_fields):
    native    — fields read from the PDF's embedded text layer only
    text      — native + Tesseract OCR regex extraction
    claude    — text + AI vision fallback
    full      — the complete pipeline: fallback documents, filing history,
                county parcel reconcile (fill / repair / veto)

Commands (all resumable / re-runnable):
    python evaluate.py classification   # page classifier vs AI labels (census DB)
    python evaluate.py build            # assemble the ground-truth manifest
    python evaluate.py extract [--limit N] [--claude-budget N]
    python evaluate.py report           # write eval/eval_results.json + eval/RESULTS.md

Privacy: eval/ artifacts contain ONLY aggregate counts and rates — no permit
numbers, addresses, or document text. Per-batch mismatch details (which DO
contain field values, for debugging) go to ~/permit_eval_mismatches.txt and
stay out of the repo.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import permit_scan as ps

HOME = os.path.expanduser("~")
EVAL_DB = os.path.join(HOME, "permit_eval.db")
CENSUS_DB = os.path.join(HOME, "permit_census.db")
MISMATCH_FILE = os.path.join(HOME, "permit_eval_mismatches.txt")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
STAGES = ("native", "text", "claude", "full")

# Eval runs must never pollute the production telemetry DB — redirect it.
ps.METRICS_DB_FILE = os.path.join(HOME, "permit_eval_metrics.db")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


class _EvalApp:
    """Headless stand-in for the tkinter App: after() runs callbacks inline,
    UI updates and archive writes are no-ops. Lets App._extract_fields run
    unmodified, exactly as it does in production."""

    def __init__(self):
        self.file_class = {}
        self.batch_verified = False
        self.last_ocr_text = ""
        self.log_lines = []
        self.eval_trace = {}

    def after(self, _delay, fn=None, *args):
        if fn:
            fn(*args)

    def _log(self, msg):
        self.log_lines.append(msg)

    def _set_progress(self, *_):
        pass

    def _update_preview(self, *_):
        pass

    def _archive_scan_text(self, *_):
        pass


def _eval_con():
    con = sqlite3.connect(EVAL_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS manifest(
        batch TEXT, orig_name TEXT, path TEXT, form_type TEXT,
        gt_permit TEXT, gt_address TEXT, gt_sbl TEXT,
        PRIMARY KEY(batch, orig_name))""")
    con.execute("""CREATE TABLE IF NOT EXISTS results(
        batch TEXT, orig_name TEXT, ran_at TEXT, seconds REAL,
        stages TEXT, final_permit TEXT, final_num TEXT, final_street TEXT,
        final_sbl TEXT, final_sources TEXT, error TEXT,
        PRIMARY KEY(batch, orig_name))""")
    return con


# ── build: assemble ground truth ──────────────────────────────────────────────

def _scan_dirs():
    dirs = list(ps.load_scan_folders())
    for extra in (r"U:\Documents\wscans", r"U:\Documents\Scans", r"F:\scan"):
        if extra not in dirs:
            dirs.append(extra)
    return dirs


def cmd_build(_args):
    arch = sqlite3.connect(ps.ARCHIVE_DB_FILE)
    rows = arch.execute(
        """SELECT orig_name, final_name, permit_id, address, sbl, form_type
           FROM scans WHERE confirmed_at IS NOT NULL AND permit_id != ''
           AND orig_name IS NOT NULL AND orig_name != ''""").fetchall()
    arch.close()

    cpaths = {}
    if os.path.exists(CENSUS_DB):
        cen = sqlite3.connect(CENSUS_DB)
        for (p,) in cen.execute("SELECT path FROM files"):
            cpaths.setdefault(os.path.basename(p), p)
        cen.close()
    dirs = _scan_dirs()

    con = _eval_con()
    con.execute("DELETE FROM manifest")
    n_files = n_missing = 0
    batches = set()
    for orig, fin, pid, addr, sbl, ft in rows:
        # Batch = permit ID. Extras get " - 2"-style final names, so keying on
        # final_name would splinter one real batch into several; the archive's
        # own audit shows permit -> parcel is unique in confirmed data.
        batch = pid
        batches.add(batch)
        path = cpaths.get(orig)
        if not (path and os.path.exists(path)):
            path = next((os.path.join(d, orig) for d in dirs
                         if os.path.exists(os.path.join(d, orig))), None)
        if path:
            n_files += 1
        else:
            n_missing += 1
        con.execute("INSERT OR REPLACE INTO manifest VALUES (?,?,?,?,?,?,?)",
                    (batch, orig, path or "", ft or "", pid, addr or "", sbl or ""))
    con.commit()
    con.close()
    print(f"manifest: {len(batches)} batches, {n_files} files located, {n_missing} missing")


# ── extract: re-run the pipeline headlessly ───────────────────────────────────

def _claude_calls_so_far():
    try:
        con = sqlite3.connect(ps.METRICS_DB_FILE)
        n = con.execute("SELECT COUNT(*) FROM events WHERE event='vision_invoked'").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def cmd_extract(args):
    con = _eval_con()
    todo = con.execute(
        """SELECT m.batch, m.orig_name, m.path FROM manifest m
           LEFT JOIN results r ON r.batch = m.batch AND r.orig_name = m.orig_name
           WHERE r.batch IS NULL AND m.path != ''
           ORDER BY m.batch""").fetchall()
    # PDFs only — images are plans by definition, never a data source
    todo = [t for t in todo if not t[2].lower().endswith(_IMAGE_EXTS)]

    if args.limit:
        allowed = []
        seen_batches = []
        for batch, orig, path in todo:
            if batch not in seen_batches:
                if len(seen_batches) >= args.limit:
                    continue
                seen_batches.append(batch)
            allowed.append((batch, orig, path))
        todo = allowed

    print(f"extract: {len(todo)} file(s) to run (claude budget {args.claude_budget})")
    claude_start = _claude_calls_so_far()

    for i, (batch, orig, path) in enumerate(todo, 1):
        spent = _claude_calls_so_far() - claude_start
        if spent >= args.claude_budget:
            print(f"stopping: claude budget reached ({spent} paid calls)")
            break
        stub = _EvalApp()
        t0 = time.time()
        err = ""
        stages = {}
        permit = num = street = sbl = ""
        final_sources = {}
        try:
            permit, num, street, sbl, final_sources = ps.App._extract_fields(stub, path)
            stages = stub.eval_trace
        except Exception as e:
            err = str(e)[:300]
        secs = round(time.time() - t0, 1)
        con.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (batch, orig, datetime.now().isoformat(timespec="seconds"), secs,
                     json.dumps(stages), permit, num, street, sbl,
                     json.dumps(final_sources), err))
        con.commit()
        status = "ERROR " + err[:40] if err else "ok"
        print(f"[{i}/{len(todo)}] {secs:6.1f}s  {status}  ({orig})")
    con.close()


# ── report: score stages against ground truth ─────────────────────────────────

def _norm_street(street):
    s = ps.normalize_suffix((street or "").upper().strip())
    return " ".join(s.replace(".", "").replace(",", "").split())


def _addr_exact(num, street, gt_num, gt_street):
    return (f"{num} {street}".strip().upper().split() ==
            f"{gt_num} {gt_street}".strip().upper().split())


def _addr_norm(num, street, gt_num, gt_street):
    return ((num or "").strip() == (gt_num or "").strip()
            and _norm_street(street) == _norm_street(gt_street))


def _stage_fields(stage, file_rows):
    """Batch-level best value per field at a pipeline stage, mimicking the
    re-OCR merge: the highest-ranked source across the batch's files wins.
    Addresses are returned as a (num, street) pair; snapshot-stage streets get
    the same split + fuzzy normalization the pipeline applies at the end, so
    stages are compared on extraction quality, not string formatting."""
    best = {"permit": ("", 0), "sbl": ("", 0), "address": (("", ""), 0)}

    def _nonempty(v):
        return any(x.strip() for x in v) if isinstance(v, tuple) else bool((v or "").strip())

    for r in file_rows:
        stages = json.loads(r["stages"] or "{}")
        if stage == "full":
            vals = {"permit": r["final_permit"], "sbl": r["final_sbl"],
                    "address": (r["final_num"] or "", r["final_street"] or "")}
            sources = json.loads(r["final_sources"] or "{}")
        else:
            snap = stages.get("text" if stage == "native" else stage)
            if not snap:
                continue
            sources = snap["sources"]
            num, street = ps.split_address(snap["address"]) if snap["address"] else ("", "")
            if street:
                street = ps.fuzzy_match_street(street)
            vals = {"permit": snap["permit"], "sbl": snap["sbl"],
                    "address": (num, street)}
            if stage == "native":  # only fields the text layer itself produced
                vals = {k: (v if sources.get(k) == "native"
                            else (("", "") if k == "address" else ""))
                        for k, v in vals.items()}
        for field in best:
            val = vals.get(field)
            rank = ps._SOURCE_RANK.get(sources.get(field, ""), 0)
            cur_val, cur_rank = best[field]
            if _nonempty(val) and (rank > cur_rank or not _nonempty(cur_val)):
                best[field] = (val, rank)
    return {k: v[0] for k, v in best.items()}


def cmd_report(_args):
    con = _eval_con()
    con.row_factory = sqlite3.Row
    manifest = con.execute("SELECT * FROM manifest").fetchall()
    results = con.execute("SELECT * FROM results WHERE error = ''").fetchall()
    errors = con.execute("SELECT COUNT(*) FROM results WHERE error != ''").fetchone()[0]

    by_batch = {}
    for r in results:
        by_batch.setdefault(r["batch"], []).append(r)
    gt = {}
    required = {}   # batch -> set of PDF files that must be extracted
    for m in manifest:
        gt[m["batch"]] = m  # any row: gt fields identical across the batch
        if m["path"] and not m["path"].lower().endswith(_IMAGE_EXTS):
            required.setdefault(m["batch"], set()).add(m["orig_name"])
    # Score only fully-extracted batches — judging a batch on a subset of its
    # files would blame the pipeline for data it never saw
    incomplete = [b for b in by_batch
                  if required.get(b, set()) - {r["orig_name"] for r in by_batch[b]}]
    for b in incomplete:
        del by_batch[b]

    stage_stats = {s: {f: {"found": 0, "exact": 0, "norm": 0}
                       for f in ("permit", "address", "sbl")} for s in STAGES}
    complete = {s: 0 for s in STAGES}
    n_batches = 0
    mismatches = []

    for batch, rows in by_batch.items():
        g = gt.get(batch)
        if g is None:
            continue
        n_batches += 1
        gt_num, gt_street = ps.split_address(g["gt_address"]) if g["gt_address"] else ("", "")
        for stage in STAGES:
            vals = _stage_fields(stage, rows)
            ok = {}
            # permit / sbl: exact, case-insensitive
            for field, gt_val in (("permit", g["gt_permit"]), ("sbl", g["gt_sbl"])):
                v = vals[field]
                st = stage_stats[stage][field]
                if v:
                    st["found"] += 1
                    hit = v.strip().upper() == (gt_val or "").strip().upper()
                    st["exact"] += hit
                    st["norm"] += hit
                    ok[field] = hit
                else:
                    ok[field] = False
            # address: exact + suffix-normalized
            num, street = vals["address"]
            st = stage_stats[stage]["address"]
            if (num or street).strip():
                st["found"] += 1
                st["exact"] += _addr_exact(num, street, gt_num, gt_street)
                hit = _addr_norm(num, street, gt_num, gt_street)
                st["norm"] += hit
                ok["address"] = hit
            else:
                ok["address"] = False
            if all(ok.values()):
                complete[stage] += 1
            if stage == "full" and not all(ok.values()):
                mismatches.append((batch, vals, ok))

    # Local-only mismatch detail (contains real values — never in the repo)
    with open(MISMATCH_FILE, "w", encoding="utf-8") as f:
        for batch, vals, ok in mismatches:
            g = gt[batch]
            f.write(f"{batch}\n")
            for field, gval in (("permit", g["gt_permit"]),
                                ("address", g["gt_address"]), ("sbl", g["gt_sbl"])):
                got = vals[field]
                if isinstance(got, tuple):
                    got = " ".join(x for x in got if x).strip()
                mark = "OK " if ok[field] else "MISS"
                f.write(f"  {mark} {field}: got {got!r} expected {gval!r}\n")

    # Telemetry from the eval runs
    cost = {"vision_calls": 0, "cache_hits": 0, "est_cost_usd": 0.0}
    try:
        mcon = sqlite3.connect(ps.METRICS_DB_FILE)
        cost["vision_calls"] = mcon.execute(
            "SELECT COUNT(*) FROM events WHERE event='vision_invoked'").fetchone()[0]
        cost["cache_hits"] = mcon.execute(
            "SELECT COUNT(*) FROM events WHERE event='vision_cache_hit'").fetchone()[0]
        cost["est_cost_usd"] = round(mcon.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM events WHERE event='vision_invoked'"
        ).fetchone()[0], 4)
        mcon.close()
    except Exception:
        pass
    avg_secs = round(sum(r["seconds"] for r in results) / len(results), 1) if results else 0

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "Re-ran the production extraction pipeline headlessly on the original "
                  "scan files of human-confirmed filings; confirmed field values are the "
                  "ground truth. Batch-level scoring mirrors the app's source-rank merge.",
        "dataset": {"batches_evaluated": n_batches,
                    "files_evaluated": len(results),
                    "file_errors": errors,
                    "avg_seconds_per_file": avg_secs},
        "eval_run_cost": cost,
        "stages": {},
        "classification": _classification_metrics(),
    }
    for stage in STAGES:
        out["stages"][stage] = {
            "batches_all_three_fields_correct": (
                round(complete[stage] / n_batches, 4) if n_batches else None),
        }
        for field in ("permit", "address", "sbl"):
            st = stage_stats[stage][field]
            out["stages"][stage][field] = {
                "coverage": round(st["found"] / n_batches, 4) if n_batches else None,
                "accuracy_when_found": (
                    round(st["norm"] / st["found"], 4) if st["found"] else None),
                "accuracy_overall": round(st["norm"] / n_batches, 4) if n_batches else None,
                **({"exact_match_overall": round(st["exact"] / n_batches, 4)}
                   if field == "address" and n_batches else {}),
            }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "eval_results.json"), "w") as f:
        json.dump(out, f, indent=2)
    _write_markdown(out)
    con.close()
    print(f"report: {n_batches} batches scored -> eval/eval_results.json, eval/RESULTS.md")
    print(f"mismatch detail (values, local only) -> {MISMATCH_FILE}")


# ── classification: page classifier vs AI labels (census DB) ──────────────────

_LOCAL_CLASSES = ("official", "application", "orange_cover", "plans",
                  "inspection_sheet", "blank_or_unreadable", "other")


def _map_ai_type(t):
    t = (t or "").lower()
    if not t:
        return None
    if "application" in t:
        return "application"
    if "orange" in t or "folder cover" in t:
        return "orange_cover"
    if "inspection procedure" in t:
        return "inspection_sheet"
    if "building permit" in t or t.strip() in ("permit", "official permit"):
        return "official"
    if ("survey" in t or "site plan" in t or "drawing" in t
            or "blueprint" in t or "construction plan" in t or t.strip() == "map"):
        return "plans"
    if "blank" in t or "unreadable" in t or "illegible" in t:
        return "blank_or_unreadable"
    return "other"


def _classification_metrics():
    """The census's AI labeling pass covered exactly the pages the local
    classifier could NOT identify — so there is no overlap for a confusion
    matrix. What the data does support is a missed-forms estimate: any
    locally-unclassified page the AI labeler called a permit form is a
    candidate local-classifier miss, so estimated recall for class C is
    local_count / (local_count + ai_found_in_unclassified_tail)."""
    if not os.path.exists(CENSUS_DB):
        return {"note": "census DB not present"}
    cen = sqlite3.connect(CENSUS_DB)
    local_counts = dict(cen.execute("SELECT cls, COUNT(*) FROM pages GROUP BY cls"))
    tail = {}
    for (ai,) in cen.execute("""SELECT ai_type FROM pages
                                WHERE ai_type IS NOT NULL AND ai_type != ''"""):
        mapped = _map_ai_type(ai) or "other"
        tail[mapped] = tail.get(mapped, 0) + 1
    cen.close()

    total = sum(local_counts.values())
    classified = total - local_counts.get("unclassified", 0)
    est_recall = {}
    for cls in ("official", "application", "orange_cover",
                "inspection_sheet", "plans"):
        found = local_counts.get(cls, 0)
        missed = tail.get(cls, 0)
        est_recall[cls] = {
            "found_by_local_classifier": found,
            "candidate_misses_in_ai_labeled_tail": missed,
            "estimated_recall": (round(found / (found + missed), 4)
                                 if found + missed else None),
        }
    return {
        "note": "The AI labeling pass covered only the pages the local "
                "classifier abstained on, so this is a missed-forms estimate, "
                "not a confusion matrix: pages in the unclassified tail that "
                "the AI called a form are candidate local misses (upper bound — "
                "AI labels are keyword-mapped and themselves imperfect).",
        "pages_total": total,
        "pages_locally_classified": classified,
        "local_coverage": round(classified / total, 4) if total else None,
        "local_class_counts": {k: v for k, v in sorted(
            local_counts.items(), key=lambda kv: -kv[1])},
        "estimated_recall": est_recall,
        "ai_labeled_tail_composition": {k: v for k, v in sorted(
            tail.items(), key=lambda kv: -kv[1])},
    }


def cmd_classification(_args):
    print(json.dumps(_classification_metrics(), indent=2))


# ── markdown report ───────────────────────────────────────────────────────────

def _write_markdown(out):
    L = []
    L.append("# Extraction Quality — Evaluation Results\n")
    L.append(f"*Generated {out['generated_at']} — see `evaluate.py` for methodology.*\n")
    d = out["dataset"]
    L.append(f"Ground truth: **{d['batches_evaluated']} human-confirmed permit batches** "
             f"({d['files_evaluated']} scan files re-processed, "
             f"{d['avg_seconds_per_file']}s avg/file). "
             "Confirmed filings are the reference: a staff member reviewed every "
             "value before filing, correcting any the pipeline got wrong.\n")
    L.append("## Accuracy by pipeline stage\n")
    L.append("Each stage adds one tier of the cascade. *Coverage* = share of batches "
             "where the stage produced a value; *accuracy* = share of batches where "
             "the value matched the confirmed one (addresses suffix-normalized).\n")
    L.append("| Stage | Permit ID | Address | SBL | All 3 correct |")
    L.append("|---|---|---|---|---|")
    label = {"native": "Native PDF text", "text": "+ Tesseract OCR",
             "claude": "+ AI vision", "full": "Full pipeline (+ county verify)"}
    for stage in STAGES:
        s = out["stages"][stage]
        cells = []
        for field in ("permit", "address", "sbl"):
            f = s[field]
            acc = f["accuracy_overall"]
            cov = f["coverage"]
            cells.append(f"{acc:.0%} (cov {cov:.0%})" if acc is not None else "—")
        allc = s["batches_all_three_fields_correct"]
        L.append(f"| {label[stage]} | " + " | ".join(cells) +
                 f" | {allc:.0%} |" if allc is not None else " | — |")
    L.append("")
    c = out["classification"]
    if "estimated_recall" in c:
        L.append("## Page classification — missed-forms estimate\n")
        L.append(f"*{c['note']}*\n")
        L.append(f"Local classifier identified {c['pages_locally_classified']:,} of "
                 f"{c['pages_total']:,} census pages ({c['local_coverage']:.0%}); "
                 "the remainder was AI-cataloged.\n")
        L.append("| Form class | Found locally | Candidate misses in tail | Est. recall |")
        L.append("|---|---|---|---|")
        for cls, m in c["estimated_recall"].items():
            er = m["estimated_recall"]
            L.append(f"| {cls} | {m['found_by_local_classifier']} | "
                     f"{m['candidate_misses_in_ai_labeled_tail']} | "
                     f"{er:.1%} |" if er is not None else
                     f"| {cls} | {m['found_by_local_classifier']} | "
                     f"{m['candidate_misses_in_ai_labeled_tail']} | — |")
        L.append("")
    cost = out["eval_run_cost"]
    L.append(f"*Eval run cost: {cost['vision_calls']} paid vision calls, "
             f"{cost['cache_hits']} cache hits, ~${cost['est_cost_usd']:.2f} API spend.*\n")
    with open(os.path.join(OUT_DIR, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("classification")
    sub.add_parser("build")
    px = sub.add_parser("extract")
    px.add_argument("--limit", type=int, default=0, help="max batches this run")
    px.add_argument("--claude-budget", type=int, default=25,
                    help="max PAID vision calls before stopping (cache hits free)")
    sub.add_parser("report")
    args = p.parse_args()
    {"classification": cmd_classification, "build": cmd_build,
     "extract": cmd_extract, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
