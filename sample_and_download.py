"""
sample_and_download.py
----------------------
Task 1 - random sample of unique branches
  Reads every JSON file under the SAMPLE_PREFIXES folders, collects the unique branches (by branchId),
  and picks SAMPLE_SIZE of them at random. For each picked branch the first record found is kept, with
  all its fields.
  Output (OUT_DIR): branches_sample.xlsx, with only the columns listed in SAMPLE_COLUMNS.
  shopArea_name comes from the area map file (AREA_MAP_PATH: area_id, area_name) and shopCity_name from
  the city map in this script.

Task 2 - plain download of one folder
  Downloads every file under DOWNLOAD_PREFIX exactly as it is (same names and sub-folders) into
  DOWNLOAD_DIR. The downloaded files are never modified.
  In addition, every record of the downloaded JSON files is written to one Excel file with the extra
  column location_url (output in TABLE_OUT_DIR).

location_url = https://www.google.com/maps?q=<latitude>,<longitude>   (empty when a coordinate is missing)
"""

import json
import os
import random
import sys

import boto3
import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]

BASE = "merged-restaurant-info/year=2025/month=09/day=17/"
DEFAULT_SAMPLE_PREFIXES = "\n".join([
    BASE + "62-ardhiya/",
    BASE + "67-jahra-waha/",
    BASE + "68-jahra-oyoun/",
    BASE + "6858-ardhiya-government/",
    BASE + "10652-jahra-jahra-industrial-area/",
])
SAMPLE_PREFIXES = [
    p.strip() if p.strip().endswith("/") else p.strip() + "/"
    for p in os.environ.get("SAMPLE_PREFIXES", DEFAULT_SAMPLE_PREFIXES).replace(",", "\n").splitlines()
    if p.strip()
]
SAMPLE_SIZE = int(os.environ.get("SAMPLE_SIZE", "70"))  # total number of unique branches
SEED = os.environ.get("SEED", "")  # empty = random seed (it is printed so a run can be repeated)
OUT_DIR = os.environ.get("OUT_DIR", "sample_output")

DOWNLOAD_PREFIX = os.environ.get("DOWNLOAD_PREFIX", BASE + "6-farwaniya/")
if not DOWNLOAD_PREFIX.endswith("/"):
    DOWNLOAD_PREFIX += "/"
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloaded_files")
TABLE_OUT_DIR = os.environ.get("TABLE_OUT_DIR", "farwaniya_output")  # Excel of the downloaded records

# area map file (xlsx or csv) with the columns area_id, area_name; used for shopArea_name
AREA_MAP_PATH = os.environ.get("AREA_MAP_PATH", "shoparea_and_id.xlsx")
AREA_MAP_ID_COL = os.environ.get("AREA_MAP_ID_COL", "area_id")
AREA_MAP_LABEL_COL = os.environ.get("AREA_MAP_LABEL_COL", "area_name")
CITY_LABELS = {
    "1": "Capital / Kuwait City",
    "2": "Hawalli",
    "3": "Farwaniya",
    "4": "Ahmadi",
    "5": "Jahra",
    "6": "Mubarak Al-Kabeer",
    "8": "Other / Unmapped",
}

# columns of branches_sample.xlsx, in this order
SAMPLE_COLUMNS = [
    "sample_no", "source_folder", "source_file", "id", "name", "restaurantSlug", "branchId", "branchName",
    "branchSlug", "branchUrl", "shopArea", "shopCity", "shopArea_name", "shopCity_name", "latitude",
    "longitude", "location_url", "cuisineString", "rate", "totalReviews", "totalRatings", "deliveryFee",
    "minimumOrderAmount", "avgDeliveryTime", "deliveryTime",
]

RUN_SAMPLE = os.environ.get("RUN_SAMPLE", "1") == "1"
RUN_DOWNLOAD = os.environ.get("RUN_DOWNLOAD", "1") == "1"

s3 = boto3.client(
    "s3", endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY, region_name="auto",
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def list_objects(prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):
                yield obj


def extract_records(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("records", "data", "restaurants", "items", "vendors"):
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def folder_name(prefix):
    return prefix.rstrip("/").split("/")[-1]


def excel_value(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def location_url(rec):
    """Google Maps link from the record's latitude / longitude (None if either is missing)."""
    lat, lng = rec.get("latitude"), rec.get("longitude")
    if any(v is None or str(v).strip() in ("", "None", "nan") for v in (lat, lng)):
        return None
    return f"https://www.google.com/maps?q={str(lat).strip()},{str(lng).strip()}"


def load_area_map():
    if not os.path.exists(AREA_MAP_PATH):
        print(f"[WARNING] area map file not found: {AREA_MAP_PATH} -> shopArea_name will be empty")
        return None
    am = (pd.read_csv(AREA_MAP_PATH, dtype=str) if AREA_MAP_PATH.lower().endswith(".csv")
          else pd.read_excel(AREA_MAP_PATH, dtype=str))
    am = am.dropna(subset=[AREA_MAP_ID_COL, AREA_MAP_LABEL_COL])
    return dict(zip(am[AREA_MAP_ID_COL].str.strip(), am[AREA_MAP_LABEL_COL].str.strip()))


def as_values(v):
    """Scalar or list -> list of clean strings (whole numbers without the .0)."""
    items = v if isinstance(v, (list, tuple, set)) else [v]
    out = []
    for x in items:
        if x is None or str(x).strip() in ("", "None", "nan"):
            continue
        t = str(x).strip()
        out.append(str(int(float(t))) if t.replace(".", "", 1).isdigit() and float(t) == int(float(t)) else t)
    return out


def write_excel(path, sheet, df):
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        out = df.apply(lambda c: c.map(excel_value))
        out.to_excel(w, sheet_name=sheet, index=False)
        ws = w.sheets[sheet]
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font = Font(bold=True)
        for i, col in enumerate(out.columns, 1):
            longest = max([len(str(col))] + [len(str(v)) for v in out[col].head(100)])
            ws.column_dimensions[get_column_letter(i)].width = min(longest + 2, 50)


def ordered_columns(rows, first):
    cols = [c for c in first if any(c in r for r in rows)]
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    return cols


# ----------------------------------------------------------------------------
# Task 1: random sample of unique branches
# ----------------------------------------------------------------------------
def sample_branches():
    print("=" * 72)
    print("TASK 1: random sample of unique branches")
    print("=" * 72)
    seen = {}  # branchId -> (record, folder, file)
    for prefix in SAMPLE_PREFIXES:
        files = [o["Key"] for o in list_objects(prefix) if o["Key"].lower().endswith(".json")]
        records = 0
        before = len(seen)
        for key in files:
            data = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read())
            for r in extract_records(data):
                records += 1
                b = str(r.get("branchId")).strip()
                if b in ("", "None", "nan") or b in seen:
                    continue
                seen[b] = (r, folder_name(prefix), os.path.basename(key))
        print(f"  {folder_name(prefix):<40} files {len(files):>3} | records {records:>7,} "
              f"| new unique branches {len(seen) - before:>5,}")

    pool = sorted(seen)
    print(f"\nUnique branches in the pool: {len(pool):,}")
    if not pool:
        print("Nothing to sample - check SAMPLE_PREFIXES.")
        return
    seed = int(SEED) if SEED else random.SystemRandom().randrange(10**9)
    k = min(SAMPLE_SIZE, len(pool))
    if k < SAMPLE_SIZE:
        print(f"[WARNING] only {len(pool)} unique branches available, sampling all of them")
    picked = random.Random(seed).sample(pool, k)
    print(f"Sampled {k} branches (seed {seed}; set SEED={seed} to get the same sample again)")

    area_map = load_area_map()
    rows = []
    for i, b in enumerate(picked, 1):
        rec, folder, fname = seen[b]
        areas, cities = as_values(rec.get("shopArea")), as_values(rec.get("shopCity"))
        row = {"sample_no": i, "source_folder": folder, "source_file": fname, **rec,
               "location_url": location_url(rec),
               "shopArea_name": (", ".join(area_map.get(a, "Unmapped") for a in areas) or None)
               if area_map is not None else None,
               "shopCity_name": ", ".join(CITY_LABELS.get(c, "Unmapped") for c in cities) or None}
        rows.append({c: row.get(c) for c in SAMPLE_COLUMNS})
    df = pd.DataFrame(rows, columns=SAMPLE_COLUMNS)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "branches_sample.xlsx")
    write_excel(path, "branches_sample", df)

    print("\nSample by folder:")
    print(df["source_folder"].value_counts().rename_axis("folder").reset_index(name="branches").to_string(index=False))
    print(f"\nSaved -> {path}")


# ----------------------------------------------------------------------------
# Task 2: download a folder as it is
# ----------------------------------------------------------------------------
def download_folder():
    print("\n" + "=" * 72)
    print(f"TASK 2: download {DOWNLOAD_PREFIX}")
    print("=" * 72)
    n, total = 0, 0
    for obj in list_objects(DOWNLOAD_PREFIX):
        rel = obj["Key"][len(DOWNLOAD_PREFIX):]
        local = os.path.join(DOWNLOAD_DIR, rel)
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        s3.download_file(R2_BUCKET, obj["Key"], local)
        n += 1
        total += obj["Size"]
        print(f"  {rel}  ({obj['Size'] / 1024:,.1f} KB)")
    if n == 0:
        print("No files found under this prefix - check DOWNLOAD_PREFIX.")
    print(f"\nDownloaded {n} file(s), {total / 1024 / 1024:,.2f} MB -> {DOWNLOAD_DIR}/")
    records_table()


def records_table():
    """One Excel with every record of the downloaded JSON files + location_url (files stay untouched)."""
    rows = []
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for fn in sorted(files):
            if not fn.lower().endswith(".json"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    recs = extract_records(json.load(f))
            except Exception as e:  # not a JSON we can read: skip it, the file itself is already downloaded
                print(f"  [skip] {path}: {e}")
                continue
            rel = os.path.relpath(path, DOWNLOAD_DIR)
            rows += [{"source_file": rel, **r, "location_url": location_url(r)} for r in recs]
    if not rows:
        print("No JSON records found, no Excel written.")
        return
    first = ["source_file", "branchId", "branchName", "id", "name", "shopArea", "shopCity",
             "latitude", "longitude", "location_url"]
    df = pd.DataFrame(rows, columns=ordered_columns(rows, first))
    os.makedirs(TABLE_OUT_DIR, exist_ok=True)
    path = os.path.join(TABLE_OUT_DIR, f"{folder_name(DOWNLOAD_PREFIX)}_records_with_location_url.xlsx")
    write_excel(path, "records", df)
    with_url = int(df["location_url"].notna().sum())
    print(f"\nRecords table: {len(df):,} records ({df['branchId'].nunique():,} unique branches), "
          f"{with_url:,} with location_url -> {path}")


def main():
    if RUN_SAMPLE:
        sample_branches()
    if RUN_DOWNLOAD:
        download_folder()


if __name__ == "__main__":
    sys.exit(main())