"""
add_branch_url.py
-----------------
Adds two columns to the (already edited) branch_area_mapped workbook, WITHOUT touching anything else:

  branchSlug   the branch slug taken from the JSONs
  branch_url   https://www.talabat.com/kuwait/restaurant/<branchId>/<branchSlug>?aid=<shopArea>

The workbook is edited in place with openpyxl (values, formatting and the other sheets are kept) and
the two columns are appended at the end of the table, so no existing column moves.
A copy is saved to OUT_DIR; the original file is not modified.

Slug lookup in the JSON records (most frequent value wins):
  branchId + branchName + shopArea  ->  branchId + shopArea  ->  branchId only
"""

import json
import os
import re
import sys
from copy import copy

import boto3
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PREFIX = os.environ.get("R2_PREFIX", "merged-restaurant-info/year=2025/month=09/day=17/")

INPUT_PATH = os.environ.get("INPUT_PATH", "branch_area_mapped.xlsx")
INPUT_SHEET = os.environ.get("INPUT_SHEET", "")      # empty = sheet "mapped", else the first sheet
URL_AREA_COL = os.environ.get("URL_AREA_COL", "shopArea")  # column used for aid=
URL_TEMPLATE = os.environ.get(
    "URL_TEMPLATE", "https://www.talabat.com/kuwait/restaurant/{branchId}/{branchSlug}?aid={area}")
OUT_DIR = os.environ.get("OUT_DIR", "branch_url_output")

RAW_COLS = ["branchId", "branchName", "shopArea", "branchSlug"]
os.makedirs(OUT_DIR, exist_ok=True)
s3 = boto3.client(
    "s3", endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY, region_name="auto",
)


def is_none(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "None")


def key(v):
    if is_none(v):
        return ""
    s = str(v).strip()
    return str(int(float(s))) if re.fullmatch(r"-?\d+(\.0+)?", s) else s


def name_key(v):
    return "" if is_none(v) else re.sub(r"\s+", " ", str(v)).strip().casefold()


def to_list(v):
    if is_none(v):
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if not is_none(x)]
    return [str(v).strip()]


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


def load_raw():
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=R2_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                keys.append(obj["Key"])
    print(f"Found {len(keys)} JSON files under {R2_PREFIX}")
    rows = []
    for i, k in enumerate(keys, 1):
        body = s3.get_object(Bucket=R2_BUCKET, Key=k)["Body"].read()
        for r in extract_records(json.loads(body)):
            rows.append({c: r.get(c) for c in RAW_COLS})
        if i % 20 == 0 or i == len(keys):
            print(f"  loaded {i}/{len(keys)} files, {len(rows):,} records so far")
    return pd.DataFrame(rows, columns=RAW_COLS)


def build_slug_lookups(raw):
    t = raw.copy()
    t["branchId"] = t["branchId"].apply(key)
    t["nk"] = t["branchName"].apply(name_key)
    t["slug"] = t["branchSlug"].apply(lambda v: "" if is_none(v) else str(v).strip())
    t["area"] = t["shopArea"].apply(lambda v: [key(x) for x in to_list(v)])
    t = t.explode("area").dropna(subset=["area"])
    t = t[(t["branchId"] != "") & (t["area"] != "") & (t["slug"] != "")]

    def best(cols):
        g = t.groupby(cols + ["slug"]).size().reset_index(name="n")
        many = int((g.groupby(cols).size() > 1).sum())
        g = g.sort_values("n", ascending=False).drop_duplicates(cols)
        d = {}
        for row in g.itertuples(index=False):
            k = tuple(getattr(row, c) for c in cols) if len(cols) > 1 else getattr(row, cols[0])
            d[k] = row.slug
        return d, many

    l1, a1 = best(["branchId", "nk", "area"])
    l2, a2 = best(["branchId", "area"])
    l3, a3 = best(["branchId"])
    return l1, l2, l3, a1


def main():
    if not os.path.exists(INPUT_PATH):
        sys.exit(f"Input file not found: {INPUT_PATH}. Put it in the repo and set INPUT_PATH.")
    wb = load_workbook(INPUT_PATH)
    if INPUT_SHEET:
        ws = wb[INPUT_SHEET]
    else:
        ws = wb["mapped"] if "mapped" in wb.sheetnames else wb.worksheets[0]
    header = {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}
    for need in ("branchId", "branchName", URL_AREA_COL):
        if need not in header:
            sys.exit(f"Column '{need}' not found in sheet '{ws.title}'. Columns: {list(header)}")
    print(f"Sheet '{ws.title}': {ws.max_row - 1:,} rows | other sheets kept: {[s for s in wb.sheetnames if s != ws.title]}")

    raw = load_raw()
    l1, l2, l3, ambiguous = build_slug_lookups(raw)
    print(f"JSON records: {len(raw):,} | slug keys with more than one slug: {ambiguous}")

    # target columns: reuse them if they already exist (re-run), else append at the end
    last = ws.max_column
    for name in ("branchSlug", "branch_url"):
        if name not in header:
            last += 1
            header[name] = last
            cell = ws.cell(row=1, column=last, value=name)
            ref = ws.cell(row=1, column=last - 1)
            cell._style = copy(ref._style)
            ws.column_dimensions[get_column_letter(last)].width = 40 if name == "branchSlug" else 80

    levels = {"branchId+branchName+shopArea": 0, "branchId+shopArea": 0, "branchId only": 0, "no slug": 0}
    samples = []
    for r in range(2, ws.max_row + 1):
        b = key(ws.cell(row=r, column=header["branchId"]).value)
        n = name_key(ws.cell(row=r, column=header["branchName"]).value)
        a = key(ws.cell(row=r, column=header[URL_AREA_COL]).value)
        if not b:
            continue
        if (b, n, a) in l1:
            slug, lvl = l1[(b, n, a)], "branchId+branchName+shopArea"
        elif (b, a) in l2:
            slug, lvl = l2[(b, a)], "branchId+shopArea"
        elif b in l3:
            slug, lvl = l3[b], "branchId only"
        else:
            slug, lvl = None, "no slug"
        levels[lvl] += 1
        ws.cell(row=r, column=header["branchSlug"], value=slug)
        url = URL_TEMPLATE.format(branchId=b, branchSlug=slug, area=a) if slug and a else None
        ws.cell(row=r, column=header["branch_url"], value=url)
        if url and len(samples) < 3:
            samples.append(url)

    if ws.auto_filter and ws.auto_filter.ref:  # keep the filter covering the new columns
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    base = os.path.splitext(os.path.basename(INPUT_PATH))[0]
    path = os.path.join(OUT_DIR, f"{base}_with_urls.xlsx")
    wb.save(path)

    print("\nSlug lookup (rows per level):")
    for k, v in levels.items():
        print(f"  {k:<32}: {v:,}")
    print("\nSample URLs:")
    for u in samples:
        print(f"  {u}")
    print(f"\nSaved -> {path}")


if __name__ == "__main__":
    sys.exit(main())