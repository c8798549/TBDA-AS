"""
sample_and_download.py
----------------------
Task 1 - random sample of unique branches
  Reads every JSON file under the SAMPLE_PREFIXES folders, collects the unique branches (by branchId),
  and picks SAMPLE_SIZE of them at random. For each picked branch the first record found is kept, with
  all its fields.
  Output (OUT_DIR): branches_sample.xlsx (+ branches_sample.json)

Task 2 - plain download of one folder
  Downloads every file under DOWNLOAD_PREFIX exactly as it is (no parsing, same names and sub-folders)
  into DOWNLOAD_DIR.
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

    rows = []
    for i, b in enumerate(picked, 1):
        rec, folder, fname = seen[b]
        rows.append({"sample_no": i, "source_folder": folder, "source_file": fname, **rec})
    first = ["sample_no", "source_folder", "source_file", "branchId", "branchName", "id", "name",
             "shopArea", "shopCity", "latitude", "longitude"]
    cols = [c for c in first if any(c in r for r in rows)]
    cols += [c for r in rows for c in r if c not in cols and not cols.append(c)]
    df = pd.DataFrame(rows, columns=cols)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "branches_sample.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    path = os.path.join(OUT_DIR, "branches_sample.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        out = df.apply(lambda c: c.map(excel_value))
        out.to_excel(w, sheet_name="branches_sample", index=False)
        ws = w.sheets["branches_sample"]
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font = Font(bold=True)
        for i, col in enumerate(out.columns, 1):
            longest = max([len(str(col))] + [len(str(v)) for v in out[col].head(100)])
            ws.column_dimensions[get_column_letter(i)].width = min(longest + 2, 50)

    print("\nSample by folder:")
    print(df["source_folder"].value_counts().rename_axis("folder").reset_index(name="branches").to_string(index=False))
    print(f"\nSaved -> {path}\n         {os.path.join(OUT_DIR, 'branches_sample.json')}")


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


def main():
    if RUN_SAMPLE:
        sample_branches()
    if RUN_DOWNLOAD:
        download_folder()


if __name__ == "__main__":
    sys.exit(main())