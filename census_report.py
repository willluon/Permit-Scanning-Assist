import sqlite3, os, re
from collections import defaultdict

con = sqlite3.connect(os.path.join(os.path.expanduser("~"), "permit_census.db"))
q = lambda s: con.execute(s).fetchone()[0]

print("pages:", q("SELECT COUNT(*) FROM pages"),
      "| still unlabeled:", q("SELECT COUNT(*) FROM pages WHERE cls='unclassified' AND ai_type IS NULL AND chars>=25"))
print()

def norm(t):
    t = re.sub(r"[^a-z0-9 ]+", "", (t or "?").lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"s$", "", t)          # crude plural fold
    return t or "?"

agg = defaultdict(lambda: [0, set(), 0, 0, 0, defaultdict(int)])
for path, ai_type, ai_fields, useful, pn, sbl in con.execute(
        """SELECT path, ai_type, ai_fields, ai_useful, permit_no, sbl
           FROM pages WHERE ai_type IS NOT NULL"""):
    a = agg[norm(ai_type)]
    a[0] += 1
    a[1].add(path)
    a[2] += useful or 0
    a[3] += 1 if (pn or "") != "" else 0
    a[4] += 1 if (sbl or "") != "" else 0
    a[5][ai_type] += 1

rows = sorted(agg.items(), key=lambda kv: -kv[1][0])
print(f"{'type (top raw name)':45s} {'pages':>5s} {'files':>5s} {'useful':>6s} {'pn':>5s} {'sbl':>5s}")
shown = 0
for key, (npg, files, useful, pn, sbl, names) in rows[:40]:
    label = max(names.items(), key=lambda kv: kv[1])[0]
    print(f"{label[:45]:45s} {npg:5d} {len(files):5d} {useful:6d} {pn:5d} {sbl:5d}")
    shown += npg
tail = sum(a[0] for _, a in rows[40:])
print(f"\n(top 40 of {len(rows)} distinct types; {tail} pages in the tail)")
