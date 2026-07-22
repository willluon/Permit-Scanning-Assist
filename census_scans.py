"""Page census over historical permit scans.

Phase 1 (scan):   classify every page of every PDF in U:\Documents\Scans
                  (June 2026 onward) with permit_scan's own local classifiers.
Phase 2 (label):  send text of still-unclassified pages to Haiku (text-only,
                  deduped by content signature) to name the document type.
Phase 3 (report): print the resulting document-type catalog.

Results persist in ~\permit_census.db so runs are resumable.
Usage: python census_scans.py [scan|label|report]
"""
import os, sys, re, json, time, sqlite3, hashlib

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # one thread per tesseract; we parallelize across processes
sys.path.insert(0, r"C:\Users\nkhoury")
import permit_scan as ps
import fitz

SRC    = r"U:\Documents\Scans"
CUTOFF = time.mktime((2026, 6, 1, 0, 0, 0, 0, 0, -1))
DB     = os.path.join(os.path.expanduser("~"), "permit_census.db")
MODEL  = "claude-haiku-4-5-20251001"


def db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, mtime REAL, pages INT, status TEXT, error TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS pages(
        path TEXT, page INT, cls TEXT, chars INT, excerpt TEXT,
        permit_no TEXT, sbl TEXT, plan_sized INT, orange INT,
        sig TEXT, ai_type TEXT, ai_fields TEXT, ai_useful INT,
        PRIMARY KEY(path, page))""")
    return con


def signature(text):
    norm = re.sub(r"[^a-z0-9]+", "", text.lower())[:400]
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def classify(text, plan, orange):
    if orange:
        return "orange_cover"
    if plan:
        return "plans"
    t = text.strip()
    if len(t) < 25:
        return "blank_or_unreadable"
    if ps._ORANGE_LABEL_RE.search(t):
        return "orange_cover"
    if ps._INSPECTION_SHEET_RE.search(t):
        return "inspection_sheet"
    kind = ps.detect_permit_type(t)
    if kind in ("official", "application"):
        return kind
    return "unclassified"


def scan(shard=0, nshards=1):
    con = db()
    files = []
    for name in os.listdir(SRC):
        if not name.lower().endswith(".pdf"):
            continue
        p = os.path.join(SRC, name)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= CUTOFF:
            files.append((p, mt))
    files.sort(key=lambda x: x[1])
    files = [f for i, f in enumerate(files) if i % nshards == shard]
    done = {r[0] for r in con.execute("SELECT path FROM files WHERE status='done'")}
    todo = [f for f in files if f[0] not in done]
    print(f"shard {shard}/{nshards}: {len(files)} PDFs in scope, {len(todo)} to process", flush=True)
    t0 = time.time()
    for i, (p, mt) in enumerate(todo):
        try:
            doc = fitz.open(p)
            rows = []
            for pg in range(len(doc)):
                page = doc[pg]
                plan = ps._plan_sized(page)
                orange = ps._orange_cover_page(page) if plan else False
                text = page.get_text().strip() if plan else ps.quick_page_text(doc, pg)
                cls = classify(text, plan, orange)
                pn = sbl = None
                try:
                    pn = ps.find_permit_number(text) if text else None
                except Exception:
                    pass
                try:
                    sbl = ps.find_sbl(text) if text else None
                except Exception:
                    pass
                rows.append((p, pg, cls, len(text), text[:1500], pn, sbl,
                             int(plan), int(orange), signature(text)))
            doc.close()
            con.executemany(
                """INSERT OR REPLACE INTO pages
                   (path,page,cls,chars,excerpt,permit_no,sbl,plan_sized,orange,sig)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""", rows)
            con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,NULL)",
                        (p, mt, len(rows), "done"))
        except Exception as e:
            con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                        (p, mt, 0, "error", repr(e)[:300]))
        con.commit()
        if (i + 1) % 25 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(todo) - i - 1) / 60
            print(f"shard {shard}: {i+1}/{len(todo)} files, {el/60:.1f} min in, ~{eta:.0f} min left", flush=True)
    print(f"shard {shard} complete", flush=True)
    if shard == 0:
        for cls, n in con.execute("SELECT cls, COUNT(*) FROM pages GROUP BY cls ORDER BY 2 DESC"):
            print(f"  {cls}: {n}", flush=True)


LABEL_PROMPT = """You are cataloging pages scanned at a town Building Department \
(Yorktown NY). Below is OCR text from {n} separate pages. For EACH page reply \
with one JSON object. Reply with ONLY a JSON array, one object per page, in order:
[{{"i": <page number as given>, "type": "<generic 2-5 word document type, e.g. \
Certificate of Occupancy, Survey Map, Contractor Insurance Certificate, \
Correspondence Letter, Site Plan Notes, Payment Receipt, Application Continuation \
Page>", "fields": [subset of "permit_no","address","sbl" actually present on the \
page], "useful": <true if this page could help identify a permit number, property \
address, or tax parcel when other sources fail, else false>}}, ...]
Use consistent generic type names so identical kinds of pages get identical names.

{pages}"""


def label(shard=0, nshards=1):
    con = db()
    with open(os.path.join(os.path.expanduser("~"), "permit_scan_config.json")) as f:
        key = json.load(f).get("anthropic_api_key", "")
    if not key:
        print("no API key in config"); return
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    sigs = con.execute(
        """SELECT sig, MIN(excerpt) FROM pages
           WHERE cls='unclassified' AND ai_type IS NULL AND chars >= 25
           GROUP BY sig ORDER BY sig""").fetchall()
    sigs = [s for i, s in enumerate(sigs) if i % nshards == shard]
    B = 10
    nb = (len(sigs) + B - 1) // B
    print(f"shard {shard}/{nshards}: {len(sigs)} signatures, {nb} batches", flush=True)
    t0 = time.time()
    for bi, start in enumerate(range(0, len(sigs), B)):
        chunk = sigs[start:start + B]
        pages = "\n\n".join(f"### PAGE {j+1}\n{ex[:1000]}"
                            for j, (sg, ex) in enumerate(chunk))
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=1500,
                messages=[{"role": "user",
                           "content": LABEL_PROMPT.format(n=len(chunk), pages=pages)}])
            raw = msg.content[0].text
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            for obj in (json.loads(m.group(0)) if m else []):
                idx = int(obj.get("i", 0)) - 1
                if 0 <= idx < len(chunk):
                    con.execute(
                        "UPDATE pages SET ai_type=?, ai_fields=?, ai_useful=? "
                        "WHERE sig=? AND cls='unclassified'",
                        (str(obj.get("type", "?"))[:60],
                         ",".join(obj.get("fields", []) or []),
                         int(bool(obj.get("useful"))), chunk[idx][0]))
            con.commit()
        except Exception as e:
            print(f"  batch {bi}: {repr(e)[:120]}", flush=True)
            time.sleep(3)
        if (bi + 1) % 20 == 0:
            el = time.time() - t0
            eta = el / (bi + 1) * (nb - bi - 1) / 60
            print(f"shard {shard}: {bi+1}/{nb} batches, ~{eta:.0f} min left", flush=True)
    print(f"shard {shard} label complete", flush=True)


def report():
    con = db()
    nf = con.execute("SELECT COUNT(*) FROM files WHERE status='done'").fetchone()[0]
    ne = con.execute("SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
    np = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    print(f"files: {nf} done, {ne} errors; pages: {np}\n")
    print("== Local classification ==")
    for cls, n in con.execute("SELECT cls, COUNT(*) FROM pages GROUP BY cls ORDER BY 2 DESC"):
        print(f"  {cls:22s} {n}")
    print("\n== Haiku catalog of unclassified pages ==")
    rows = con.execute(
        """SELECT ai_type, COUNT(*) pages, COUNT(DISTINCT path) files,
                  SUM(ai_useful), MAX(ai_fields),
                  SUM(CASE WHEN permit_no IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN sbl IS NOT NULL THEN 1 ELSE 0 END)
           FROM pages WHERE ai_type IS NOT NULL
           GROUP BY ai_type ORDER BY 2 DESC""").fetchall()
    print(f"  {'type':40s} {'pages':>5s} {'files':>5s} {'useful':>6s} {'pn_hit':>6s} {'sbl_hit':>7s}  fields")
    for t, npg, nfl, useful, fields, pn, sbl in rows:
        print(f"  {t:40s} {npg:5d} {nfl:5d} {useful or 0:6d} {pn or 0:6d} {sbl or 0:7d}  {fields or ''}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd in ("scan", "label"):
        shard = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        nshards = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        (scan if cmd == "scan" else label)(shard, nshards)
    else:
        report()
