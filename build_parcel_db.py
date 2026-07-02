"""Build yorktown_parcels.db from the NYS GIS Tax Parcel Centroid Points service.

Downloads every Yorktown (Westchester) parcel — PRINT_KEY (SBL in Yorktown's
dotted format, e.g. 48.11-1-11), address, street number, street name — into a
local SQLite database the scan app can query offline.

Run standalone to create/refresh the database:
    python build_parcel_db.py

NYS refreshes the parcel data annually from county assessment rolls; re-run
this once a year (or whenever the town's tax maps change) to stay current.
"""

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request

_HOME      = os.path.expanduser("~")
PARCEL_DB  = os.path.join(_HOME, "yorktown_parcels.db")

SERVICE_URL = (
    "https://gisservices.its.ny.gov/arcgis/rest/services/"
    "NYS_Tax_Parcel_Centroid_Points/MapServer/0/query"
)
WHERE      = "MUNI_NAME='Yorktown' AND COUNTY_NAME='Westchester'"
OUT_FIELDS = "PRINT_KEY,PARCEL_ADDR,LOC_ST_NBR,LOC_STREET,PRIMARY_OWNER"
PAGE_SIZE  = 1000


def normalize_street(street):
    """GIS street → app format: uppercase, no trailing period, single spaces.
    'Williams Dr.' → 'WILLIAMS DR' (matches yorktown_streets.txt style)."""
    if not street:
        return ""
    s = re.sub(r"\s+", " ", street.upper().strip())
    return s.rstrip(".").strip()


def _fetch_page(offset):
    params = urllib.parse.urlencode({
        "where": WHERE,
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "orderByFields": "OBJECTID",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "f": "json",
    })
    with urllib.request.urlopen(f"{SERVICE_URL}?{params}", timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return data


def download_parcels():
    rows, offset = [], 0
    while True:
        data = _fetch_page(offset)
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            a = f["attributes"]
            print_key = (a.get("PRINT_KEY") or "").strip()
            if not print_key:
                continue
            addr   = (a.get("PARCEL_ADDR") or "").strip().upper().rstrip(".")
            st_nbr = str(a.get("LOC_ST_NBR") or "").strip()
            street = normalize_street(a.get("LOC_STREET") or "")
            owner  = (a.get("PRIMARY_OWNER") or "").strip()
            rows.append((print_key, addr, st_nbr, street, owner))
        print(f"  fetched {offset + len(feats)} parcels...")
        if not data.get("exceededTransferLimit") and len(feats) < PAGE_SIZE:
            break
        offset += len(feats)
    return rows


def write_db(rows, db_path=PARCEL_DB):
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE parcels (
            print_key TEXT NOT NULL,   -- SBL, Yorktown dotted format e.g. 48.11-1-11
            addr      TEXT,            -- full parcel address e.g. 1191 WILLIAMS DR
            st_nbr    TEXT,            -- street number e.g. 1191
            street    TEXT,            -- normalized street e.g. WILLIAMS DR
            owner     TEXT             -- primary owner from assessment roll
        );
        CREATE INDEX idx_print_key ON parcels(print_key);
        CREATE INDEX idx_addr      ON parcels(st_nbr, street);
        CREATE INDEX idx_street    ON parcels(street);
    """)
    con.executemany("INSERT INTO parcels VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    if os.path.exists(db_path):
        os.remove(db_path)
    os.rename(tmp, db_path)


def main():
    print(f"Downloading Yorktown parcels from NYS GIS...")
    rows = download_parcels()
    with_addr = sum(1 for r in rows if r[2] and r[3])
    print(f"Downloaded {len(rows)} parcels ({with_addr} with addresses).")
    write_db(rows)
    print(f"Wrote {PARCEL_DB}")


if __name__ == "__main__":
    main()
