"""
branch_area_mapping.py
----------------------
Enriches the branch-area correction file with restaurant and city information taken from the JSONs.

Input file (one row per branch), columns:
  branchId, branchName, name, latitude, longitude, shopArea, shopArea_name, coord_area_id,
  name_area_id, final_area_id, final_area_name, area_basis, coords_valid

Steps:
  1) Match every row with the JSON records using branchId + branchName + shopArea
     (falls back to branchId + shopArea, then branchId alone; the log shows how many rows used each level)
     -> restaurant_id, restaurant_name (the JSON id / name) and shopCity (+ shopCity_name).
  2) Build an area -> city map from the JSONs (most frequent shopCity of each shopArea) and look up
     final_area_id in it -> final_shopCity (+ final_city_name). Only final_area_id is used for this lookup.
  3) area_in_city and branch_loc are added as empty columns.

Output (OUT_DIR): branch_area_mapped.xlsx  (sheet "mapped" + sheet "summary")
"""

import json
import os
import re
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
R2_PREFIX = os.environ.get("R2_PREFIX", "merged-restaurant-info/year=2025/month=09/day=17/")

INPUT_PATH = os.environ.get("INPUT_PATH", "q_area_validation.xlsx")
INPUT_SHEET = os.environ.get("INPUT_SHEET", "")  # empty = first sheet
OUT_DIR = os.environ.get("OUT_DIR", "branch_area_mapping_output")

CITY_LABELS = {
    "1": "Capital / Kuwait City",
    "2": "Hawalli",
    "3": "Farwaniya",
    "4": "Ahmadi",
    "5": "Jahra",
    "6": "Mubarak Al-Kabeer",
    "8": "Other / Unmapped",
}

OUTPUT_COLUMNS = [
    "restaurant_id", "restaurant_name", "branchId", "branchName", "name", "latitude", "longitude",
    "shopCity", "shopCity_name", "shopArea", "shopArea_name", "coord_area_id", "name_area_id",
    "final_area_id", "final_area_name", "final_shopCity", "final_city_name", "area_basis",
    "coords_valid", "area_in_city", "branch_loc",
]
RAW_COLS = ["id", "name", "branchId", "branchName", "shopCity", "shopArea"]

os.makedirs(OUT_DIR, exist_ok=True)
s3 = boto3.client(
    "s3", endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY, region_name="auto",
)
LOG = []


def out(text=""):
    print(text)
    LOG.append(str(text))


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def is_none(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "None")


def norm_id(v):
    """Id-like value -> int when it is a whole number, else the stripped string, else None."""
    if is_none(v):
        return None
    if isinstance(v, bool):
        return str(v)
    s = str(v).strip()
    if re.fullmatch(r"-?\d+(\.0+)?", s):
        return int(float(s))
    return s


def key(v):
    n = norm_id(v)
    return "" if n is None else str(n)


def name_key(v):
    return "" if is_none(v) else re.sub(r"\s+", " ", str(v)).strip().casefold()


def to_list(v):
    if is_none(v):
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if not is_none(x)]
    return [str(v).strip()]


def city_name(c):
    if c is None:
        return None
    return CITY_LABELS.get(str(c), "Unmapped")


# ----------------------------------------------------------------------------
# JSON files
# ----------------------------------------------------------------------------
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


def build_lookups(raw):
    """Lookup tables from the JSON records (most frequent value wins)."""
    t = raw.copy()
    t["branchId"] = t["branchId"].apply(key)
    t["nk"] = t["branchName"].apply(name_key)
    t["id"] = t["id"].apply(norm_id)
    t["area"] = t["shopArea"].apply(lambda v: [key(x) for x in to_list(v)])
    t["city"] = t["shopCity"].apply(lambda v: [key(x) for x in to_list(v)] or [""])
    t = t.explode("area").explode("city").dropna(subset=["area"])
    t = t[(t["area"] != "") & (t["branchId"] != "")]

    def best(cols, vals):
        g = t.groupby(cols + vals, dropna=False).size().reset_index(name="n")
        many = int((g.groupby(cols).size() > 1).sum())
        g = g.sort_values("n", ascending=False).drop_duplicates(cols)
        return g, many

    vals = ["id", "name", "city"]
    g1, amb1 = best(["branchId", "nk", "area"], vals)
    g2, _ = best(["branchId", "area"], vals)
    g3, _ = best(["branchId"], vals)

    def to_dict(g, cols):
        d = {}
        for row in g.itertuples(index=False):
            k = tuple(getattr(row, c) for c in cols) if len(cols) > 1 else getattr(row, cols[0])
            d[k] = (row.id, row.name, row.city or None)
        return d

    l1 = to_dict(g1, ["branchId", "nk", "area"])
    l2 = to_dict(g2, ["branchId", "area"])
    l3 = to_dict(g3, ["branchId"])

    # area -> city map (most frequent city of each area)
    ac = t[t["city"] != ""].groupby(["area", "city"]).size().reset_index(name="n")
    cities_per_area = ac.groupby("area")["city"].nunique()
    area_city = (ac.sort_values("n", ascending=False).drop_duplicates("area")
                   .set_index("area")["city"].to_dict())
    return l1, l2, l3, area_city, amb1, int((cities_per_area > 1).sum())


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not os.path.exists(INPUT_PATH):
        sys.exit(f"Input file not found: {INPUT_PATH}. Put it in the repo and set INPUT_PATH.")
    if INPUT_PATH.lower().endswith(".csv"):
        df = pd.read_csv(INPUT_PATH)
    else:
        df = pd.read_excel(INPUT_PATH, sheet_name=INPUT_SHEET or 0)
    for c in OUTPUT_COLUMNS:
        if c not in df.columns:
            df[c] = None
    out(f"Input rows: {len(df):,}   ({INPUT_PATH})")

    raw = load_raw()
    l1, l2, l3, area_city, ambiguous_keys, areas_many_cities = build_lookups(raw)
    out(f"JSON records: {len(raw):,} | areas in the area->city map: {len(area_city):,} "
        f"| areas seen under more than one city: {areas_many_cities}")

    levels = {"branchId+branchName+shopArea": 0, "branchId+shopArea": 0, "branchId only": 0, "no match": 0}
    rid, rname, scity = [], [], []
    for b, n, a in zip(df["branchId"], df["branchName"], df["shopArea"]):
        kb, kn, ka = key(b), name_key(n), key(a)
        if (kb, kn, ka) in l1:
            hit, lvl = l1[(kb, kn, ka)], "branchId+branchName+shopArea"
        elif (kb, ka) in l2:
            hit, lvl = l2[(kb, ka)], "branchId+shopArea"
        elif kb in l3:
            hit, lvl = l3[kb], "branchId only"
        else:
            hit, lvl = (None, None, None), "no match"
        levels[lvl] += 1
        rid.append(norm_id(hit[0]))
        rname.append(hit[1])
        scity.append(norm_id(hit[2]))

    res = pd.DataFrame(index=df.index)
    res["restaurant_id"], res["restaurant_name"] = rid, rname
    for c in ("branchId", "branchName", "name", "latitude", "longitude", "shopArea", "shopArea_name",
              "coord_area_id", "name_area_id", "final_area_id", "final_area_name", "area_basis", "coords_valid"):
        res[c] = df[c]
    for c in ("branchId", "shopArea", "coord_area_id", "name_area_id", "final_area_id"):
        res[c] = res[c].apply(norm_id)
    for c in ("latitude", "longitude"):
        res[c] = pd.to_numeric(res[c], errors="coerce")

    res["shopCity"] = scity
    res["shopCity_name"] = res["shopCity"].apply(city_name)

    # final city: only from final_area_id
    fcity = [norm_id(area_city.get(key(a))) for a in res["final_area_id"]]
    res["final_shopCity"] = fcity
    res["final_city_name"] = res["final_shopCity"].apply(city_name)
    res["area_in_city"] = None  # left empty on purpose
    res["branch_loc"] = None    # left empty on purpose
    res = res[OUTPUT_COLUMNS]

    # ---------------- log ----------------
    out("\nMatch with the JSON records (rows per level):")
    for k, v in levels.items():
        out(f"  {k:<32}: {v:,}")
    out(f"  keys with more than one JSON value (id/name/city): {ambiguous_keys:,}  -> most frequent was used")
    n = len(res)
    not_found = int(res["final_shopCity"].isna().sum())
    changed = int(((res["shopCity"].astype(str) != res["final_shopCity"].astype(str))
                   & res["final_shopCity"].notna() & res["shopCity"].notna()).sum())
    out("\nFinal area -> city:")
    out(f"  final_area_id found in a shopCity : {n - not_found:,}")
    out(f"  final_area_id NOT found           : {not_found:,}")
    out(f"  final_shopCity differs from shopCity : {changed:,}")
    out("\nRows by area_basis:")
    out(res["area_basis"].value_counts(dropna=False).rename_axis("area_basis").reset_index(name="rows").to_string(index=False))

    summary = pd.DataFrame(
        [(f"match: {k}", v) for k, v in levels.items()]
        + [("rows", n), ("final_area_id found in a shopCity", n - not_found),
           ("final_area_id not found", not_found), ("final_shopCity differs from shopCity", changed),
           ("JSON keys with more than one value", ambiguous_keys)],
        columns=["metric", "value"])

    path = os.path.join(OUT_DIR, "branch_area_mapped.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, d in (("mapped", res), ("summary", summary)):
            d.to_excel(w, sheet_name=name, index=False)
            ws = w.sheets[name]
            ws.freeze_panes = "A2"
            for c in ws[1]:
                c.font = Font(bold=True)
            for i, col in enumerate(d.columns, 1):
                longest = max([len(str(col))] + [len(str(v)) for v in d[col].head(200)])
                ws.column_dimensions[get_column_letter(i)].width = min(longest + 2, 60)
    with open(os.path.join(OUT_DIR, "branch_area_mapping_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    out(f"\nSaved -> {path}")


if __name__ == "__main__":
    sys.exit(main())