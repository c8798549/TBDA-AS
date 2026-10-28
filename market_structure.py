"""
market_structure.py
-------------------
Market / Restaurant Structure analysis.

Sources:
  1) The brand-grouping file (sheet "restaurants", created by build_restaurants_sheet.py):
     one row per restaurant (brand), with the list of its branches (branchId)
     and branch names (branchName). This defines what a restaurant is and which branches it owns.
  2) The JSON files on R2: used only to look up shopCity / shopArea / cuisineString / verticalType
     for each branch (by branchId). Records whose branch is not in the file are ignored.

Outputs (OUT_DIR):
  market_structure_report.txt        same text as the console log
  market_structure_summary.xlsx      Overview + branch distribution + area density + Data_Quality
  branches_per_restaurant.xlsx       all restaurants (id, name, branch count, branch ids, branch names)
  multi_branch_restaurants.xlsx      same layout, multi-branch restaurants only
  restaurants_by_city.xlsx           restaurants / branches per city (with label)
  branches_by_area.xlsx              restaurants / branches per area
  restaurants_by_activity.xlsx       one sheet by cuisine + one sheet by verticalType
"""

import io
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
R2_PREFIX = os.environ.get(
    "R2_PREFIX", "merged-restaurant-info/year=2025/month=09/day=17/"
)

MAPPING_R2_KEY = os.environ.get("MAPPING_R2_KEY", "")
MAPPING_PATH = os.environ.get("MAPPING_PATH", "unique_restaurants_grouped_by_brand.xlsx")
MAPPING_SHEET = os.environ.get("MAPPING_SHEET", "restaurants")  # sheet created by build_restaurants_sheet.py
MAPPING_ID_COL = os.environ.get("MAPPING_ID_COL", "id")
MAPPING_NAME_COL = os.environ.get("MAPPING_NAME_COL", "name")
MAPPING_BRANCH_COL = os.environ.get("MAPPING_BRANCH_COL", "branchId")
MAPPING_BRANCHNAME_COL = os.environ.get("MAPPING_BRANCHNAME_COL", "branchName")
MAPPING_AREA_COL = os.environ.get("MAPPING_AREA_COL", "shopArea")
MAPPING_AREANAME_COL = os.environ.get("MAPPING_AREANAME_COL", "area_name")

# Area label map file (xlsx or csv): columns area_id, area_name
AREA_MAP_PATH = os.environ.get("AREA_MAP_PATH", "shoparea_and_id.xlsx")
AREA_MAP_ID_COL = os.environ.get("AREA_MAP_ID_COL", "area_id")
AREA_MAP_LABEL_COL = os.environ.get("AREA_MAP_LABEL_COL", "area_name")

EXPECTED_RESTAURANTS = int(os.environ.get("EXPECTED_RESTAURANTS", "0"))  # 0 = skip the comparison
TOP_N = int(os.environ.get("TOP_N", "10"))
OUT_DIR = os.environ.get("OUT_DIR", "market_structure_output")

CITY_LABELS = {
    "1": "Capital / Kuwait City",
    "2": "Hawalli",
    "3": "Farwaniya",
    "4": "Ahmadi",
    "5": "Jahra",
    "6": "Mubarak Al-Kabeer",
    "8": "Other / Unmapped",
}

RAW_COLS = ["id", "name", "branchId", "shopCity", "shopArea", "cuisineString", "verticalType"]
os.makedirs(OUT_DIR, exist_ok=True)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 60)

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------
REPORT = []


def out(text=""):
    print(text)
    REPORT.append(str(text))


def section(title):
    out()
    out("=" * 72)
    out(title)
    out("=" * 72)


def show(df, n=None):
    d = df.head(n) if n else df
    out(d.to_string(index=False))


def kv(label, value, width=34):
    out(f"  {label:<{width}}: {value}")


def write_excel(name, sheets):
    path = os.path.join(OUT_DIR, name)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for sheet, df in sheets.items():
            sheet = sheet[:31]
            df = df.copy()
            for c in df.columns:
                if df[c].dtype == object:
                    df[c] = df[c].apply(
                        lambda v: v[:32000] if isinstance(v, str) else v
                    )
            df.to_excel(w, sheet_name=sheet, index=False)
            ws = w.sheets[sheet]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, col in enumerate(df.columns, 1):
                longest = max(
                    [len(str(col))] + [len(str(v)) for v in df[col].head(200)]
                )
                ws.column_dimensions[get_column_letter(i)].width = min(longest + 2, 60)
    return name


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
def parse_ids(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    return re.findall(r"\d+", str(v))


def parse_names(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    s = str(v).strip()
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    found = re.findall(r'"((?:[^"\\]|\\.)*)"', s)
    return found if found else [s]


def to_list(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip() not in ("", "None")]
    s = str(v).strip()
    return [] if s in ("", "None", "nan") else [s]


def is_none(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


def read_table(src, name, sheet=None):
    if str(name).lower().endswith(".csv"):
        return pd.read_csv(src, dtype=str)
    return pd.read_excel(src, sheet_name=sheet if sheet is not None else 0, dtype=str)


# ----------------------------------------------------------------------------
# 1) Manual validation file
# ----------------------------------------------------------------------------
def load_mapping():
    if MAPPING_R2_KEY:
        src = io.BytesIO(s3.get_object(Bucket=R2_BUCKET, Key=MAPPING_R2_KEY)["Body"].read())
        name = MAPPING_R2_KEY
    else:
        src = name = MAPPING_PATH
        if not os.path.splitext(name)[1]:
            for ext in (".xlsx", ".csv"):
                if os.path.exists(name + ext):
                    src = name = name + ext
                    break
        if not os.path.exists(name):
            sys.exit(f"Mapping file not found: {name}. Put it in the repo and set "
                     "MAPPING_PATH, or upload it to R2 and set MAPPING_R2_KEY.")

    sheet_used = "csv"
    if not name.lower().endswith(".csv"):
        xl = pd.ExcelFile(src)
        if MAPPING_SHEET:
            sheet_used = MAPPING_SHEET
        else:
            cands = [s for s in xl.sheet_names if s.lower() == "restaurants"] or \
                    [s for s in xl.sheet_names if "unique" in s.lower()]
            sheet_used = cands[0] if cands else xl.sheet_names[0]
        if hasattr(src, "seek"):
            src.seek(0)
    m = read_table(src, name, sheet_used if sheet_used != "csv" else None)

    m[MAPPING_ID_COL] = m[MAPPING_ID_COL].astype(str).str.strip()
    dup_ids = m[m.duplicated(MAPPING_ID_COL, keep=False)][MAPPING_ID_COL].unique().tolist()
    m = m.drop_duplicates(MAPPING_ID_COL, keep="first").reset_index(drop=True)

    has_names = MAPPING_BRANCHNAME_COL in m.columns
    rows = []
    for _, r in m.iterrows():
        ids = parse_ids(r[MAPPING_BRANCH_COL])
        names = parse_names(r[MAPPING_BRANCHNAME_COL]) if has_names else []
        aligned = len(names) == len(ids)
        seen, o_ids, o_names = set(), [], []
        for i, b in enumerate(ids):
            if b in seen:
                continue
            seen.add(b)
            o_ids.append(b)
            o_names.append(names[i] if aligned else "")
        rows.append({
            "restaurant_id": r[MAPPING_ID_COL],
            "restaurant": r[MAPPING_NAME_COL],
            "branch_ids": o_ids,
            "branch_names": o_names,
        })
    rests = pd.DataFrame(rows)

    # raw id -> restaurant (via the all_ids column, to tell whether several ids belong to the same restaurant)
    id_to_rest = {}
    if "all_ids" in m.columns:
        for rid, a in zip(m[MAPPING_ID_COL], m["all_ids"]):
            for i in parse_ids(a):
                id_to_rest.setdefault(i, rid)
    for rid in rests["restaurant_id"]:
        id_to_rest.setdefault(rid, rid)

    # area labels (optional)
    area_map, area_src = {}, "none"
    if AREA_MAP_PATH and os.path.exists(AREA_MAP_PATH):
        am = read_table(AREA_MAP_PATH, AREA_MAP_PATH)
        am = am.dropna(subset=[AREA_MAP_ID_COL, AREA_MAP_LABEL_COL])
        area_map = dict(zip(am[AREA_MAP_ID_COL].astype(str).str.strip(),
                            am[AREA_MAP_LABEL_COL].astype(str).str.strip()))
        area_src = AREA_MAP_PATH
    elif MAPPING_AREA_COL in m.columns and MAPPING_AREANAME_COL in m.columns:
        t = m[[MAPPING_AREA_COL, MAPPING_AREANAME_COL]].dropna()
        t[MAPPING_AREA_COL] = t[MAPPING_AREA_COL].astype(str).str.strip()
        area_map = (t.groupby(MAPPING_AREA_COL)[MAPPING_AREANAME_COL]
                      .agg(lambda x: x.value_counts().index[0]).to_dict())
        area_src = f"{sheet_used} sheet ({MAPPING_AREANAME_COL})"
    return rests, dup_ids, area_map, area_src, sheet_used, id_to_rest


# ----------------------------------------------------------------------------
# 2) Raw JSON
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
    for i, key in enumerate(keys, 1):
        body = s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()
        for r in extract_records(json.loads(body)):
            rows.append({c: r.get(c) for c in RAW_COLS})
        if i % 20 == 0 or i == len(keys):
            print(f"  loaded {i}/{len(keys)} files, {len(rows):,} records so far")
    return pd.DataFrame(rows, columns=RAW_COLS), len(keys)


# ----------------------------------------------------------------------------
# 3) Branch attribute tables  (branchId, value)  one row per distinct pair
# ----------------------------------------------------------------------------
def attr_table(raw, col, comma_split=False, casefold=False):
    t = raw[["branchId", col]].copy()
    if comma_split:
        t[col] = t[col].fillna("").astype(str).str.split(",")
    else:
        t[col] = t[col].apply(to_list)
    t = t.explode(col).dropna(subset=[col])
    t[col] = t[col].astype(str).str.strip()
    if casefold:
        t[col] = t[col].str.casefold()
    t = t[~t[col].str.lower().isin(["", "none", "nan"])]
    return t.drop_duplicates()


def dist_table(pairs, attr, col, total_restaurants):
    j = pairs.merge(attr, on="branchId")
    g = (j.groupby(col)
           .agg(restaurants=("restaurant_id", "nunique"), branches=("branchId", "nunique"))
           .reset_index())
    g["pct_of_restaurants"] = (g["restaurants"] / total_restaurants * 100).round(2)
    g["branches_per_restaurant"] = (g["branches"] / g["restaurants"]).round(2)
    without = int(total_restaurants - j["restaurant_id"].nunique())
    return g.sort_values(["restaurants", "branches"], ascending=False).reset_index(drop=True), without


# ----------------------------------------------------------------------------
# 4) Main
# ----------------------------------------------------------------------------
def main():
    rests, dup_ids, area_map, area_src, sheet_used, id_to_rest = load_mapping()
    raw, n_files = load_raw()
    total_records = len(raw)

    # ---------------- pairs (restaurant, branch) ----------------
    pairs = (rests[["restaurant_id", "branch_ids"]]
             .explode("branch_ids").dropna()
             .rename(columns={"branch_ids": "branchId"})
             .drop_duplicates())
    name_of = dict(zip(rests["restaurant_id"], rests["restaurant"]))

    total_restaurants = len(rests)
    total_branches = pairs["branchId"].nunique()
    rests["branches"] = rests["branch_ids"].apply(len)
    bpr = rests.set_index("restaurant_id")["branches"]
    sum_branch_counts = int(bpr.sum())

    # branches linked to more than one restaurant in the validation file
    per_branch = pairs.groupby("branchId")["restaurant_id"].agg(list)
    shared = per_branch[per_branch.apply(len) > 1]
    shared_df = pd.DataFrame({
        "branchId": shared.index,
        "restaurants_count": shared.apply(len).values,
        "restaurant_ids": shared.apply(lambda l: ", ".join(l)).values,
        "restaurant_names": shared.apply(lambda l: " | ".join(str(name_of.get(x)) for x in l)).values,
    })

    # ---------------- raw vs validation ----------------
    raw["branchId"] = raw["branchId"].astype(str).str.strip()
    raw["id"] = raw["id"].astype(str).str.strip()

    # shared branches according to the JSONs: same branchId under more than one id
    rb = raw[["branchId", "id", "name"]].drop_duplicates()
    rb = rb[~rb["id"].str.lower().isin(["", "none", "nan"])]
    gj = rb.groupby("branchId").agg(
        ids=("id", lambda x: sorted(set(x))),
        names=("name", lambda x: sorted({str(v) for v in x if not is_none(v)})),
    )
    gj = gj[gj["ids"].apply(len) > 1]
    rows_j = []
    for b, r in gj.iterrows():
        mapped = sorted({id_to_rest[i] for i in r["ids"] if i in id_to_rest})
        unmapped_n = sum(1 for i in r["ids"] if i not in id_to_rest)
        if unmapped_n:
            status = "has id not in validation file"
        elif len(mapped) == 1:
            status = "same restaurant in validation file"
        else:
            status = "different restaurants in validation file"
        rows_j.append({
            "branchId": b,
            "ids_count": len(r["ids"]),
            "ids": ", ".join(r["ids"]),
            "names": " | ".join(r["names"]),
            "restaurants_in_validation_count": len(mapped),
            "restaurant_ids_in_validation": ", ".join(mapped),
            "restaurant_names_in_validation": " | ".join(str(name_of.get(x)) for x in mapped),
            "status": status,
        })
    shared_json_df = pd.DataFrame(rows_j, columns=[
        "branchId", "ids_count", "ids", "names", "restaurants_in_validation_count",
        "restaurant_ids_in_validation", "restaurant_names_in_validation", "status"])
    valid_b = set(pairs["branchId"])
    in_map = raw["branchId"].isin(valid_b)
    dropped_records = int((~in_map).sum())
    dropped_branch_ids = sorted(raw.loc[~in_map, "branchId"].unique().tolist())
    raw = raw[in_map].copy()
    missing_in_raw = sorted(valid_b - set(raw["branchId"].unique()))

    # =======================================================================
    section("1) INPUTS & DATA CHECKS")
    kv("Validation file / sheet", f"{MAPPING_R2_KEY or MAPPING_PATH}  /  {sheet_used}")
    kv("JSON files", n_files)
    kv("JSON records", f"{total_records:,}")
    kv("Records ignored (branch not in file)", f"{dropped_records:,}  ({len(dropped_branch_ids):,} branchIds)")
    kv("Branches in file, missing from JSON", f"{len(missing_in_raw):,}")
    kv("Shared branches - validation file", f"{len(shared_df):,}  (branch under >1 restaurant)")
    kv("Shared branches - JSONs", f"{len(shared_json_df):,}  (branch under >1 id)")
    kv("Duplicate restaurant ids in sheet", len(dup_ids))
    if missing_in_raw:
        out("\n  Branches in the validation file but missing from the JSON files")
        out("  (counted in branch totals, but have no city/area/cuisine):")
        show(pd.DataFrame({
            "branchId": missing_in_raw,
            "restaurant_id": [per_branch[b][0] for b in missing_in_raw],
            "restaurant": [name_of.get(per_branch[b][0]) for b in missing_in_raw],
        }))
    if len(shared_df):
        out("\n  Shared branches from the VALIDATION file (first 10) - full list: summary xlsx, sheet Shared_Branches_Unique:")
        show(shared_df, 10)
    if len(shared_json_df):
        out("\n  Shared branches from the JSONs (first 10) - full list: summary xlsx, sheet Shared_Branches_JSON:")
        show(shared_json_df.drop(columns=["names", "restaurant_ids_in_validation"]), 10)
        out("\n  JSON shared branches by status:")
        show(shared_json_df["status"].value_counts().rename_axis("status").reset_index(name="branches"))

    # =======================================================================
    # Totals + per restaurant
    multi = bpr[bpr > 1]
    single = bpr[bpr == 1]
    avg_listed = round(float(bpr.mean()), 3)
    avg_unique = round(total_branches / total_restaurants, 3)
    median_b = float(bpr.median())

    dist_df = (bpr.value_counts().sort_index()
                  .rename_axis("branches_per_restaurant").reset_index(name="restaurants"))
    dist_df["pct_of_restaurants"] = (dist_df["restaurants"] / total_restaurants * 100).round(2)

    owned_multi = int(multi.sum())
    ratio = {
        "restaurants_to_branches_pct": round(total_restaurants / total_branches * 100, 2),
        "single_branch_restaurants": int(len(single)),
        "single_branch_restaurants_pct": round(len(single) / total_restaurants * 100, 2),
        "multi_branch_restaurants": int(len(multi)),
        "multi_branch_restaurants_pct": round(len(multi) / total_restaurants * 100, 2),
        "branches_of_multi_branch_restaurants": owned_multi,
        "branches_of_multi_branch_restaurants_pct": round(owned_multi / sum_branch_counts * 100, 2),
    }

    # per-restaurant excel tables
    def rest_table(df):
        t = pd.DataFrame({
            "restaurant_id": df["restaurant_id"],
            "restaurant_name": df["restaurant"],
            "branches_count": df["branches"],
            "branch_ids": df["branch_ids"].apply(lambda l: ", ".join(l)),
            "branch_names": df["branch_names"].apply(lambda l: " | ".join(l)),
        })
        return t.sort_values(["branches_count", "restaurant_name"], ascending=[False, True]).reset_index(drop=True)

    all_rest_t = rest_table(rests)
    multi_rest_t = rest_table(rests[rests["branches"] > 1])

    section("2) TOTALS")
    kv("Total restaurants", f"{total_restaurants:,}" + (f"   (expected {EXPECTED_RESTAURANTS:,})" if EXPECTED_RESTAURANTS else ""))
    kv("Total branches (unique branchId)", f"{total_branches:,}")
    kv("Sum of branches listed per restaurant", f"{sum_branch_counts:,}")

    section("3) BRANCHES PER RESTAURANT")
    kv("Avg branches / restaurant", f"{avg_listed}   (using unique branches: {avg_unique})")
    kv("Median", median_b)
    kv("Max", int(bpr.max()))
    kv("Multi-branch restaurants", f"{len(multi):,}  ({ratio['multi_branch_restaurants_pct']}%)")
    kv("Single-branch restaurants", f"{len(single):,}  ({ratio['single_branch_restaurants_pct']}%)")
    out(f"\n  Top {TOP_N} restaurants by branches:")
    show(all_rest_t[["restaurant_id", "restaurant_name", "branches_count"]], TOP_N)
    out("\n  Number of restaurants for each exact branch count:")
    show(dist_df)

    section("4) RESTAURANTS vs BRANCHES")
    kv("Restaurants / branches", f"{ratio['restaurants_to_branches_pct']}%")
    kv("Branches per restaurant", avg_listed)
    kv("Branches owned by multi-branch", f"{owned_multi:,}  ({ratio['branches_of_multi_branch_restaurants_pct']}% of listed branches)")

    def multi_value_branches(col, label_map):
        """Branches that have more than one value (city/area) in the JSONs, with the record count per value."""
        t = raw[["branchId", col]].copy()
        t[col] = t[col].apply(to_list)
        t = t.explode(col).dropna(subset=[col])
        t[col] = t[col].astype(str).str.strip()
        t = t[~t[col].str.lower().isin(["", "none", "nan"])]
        c = t.groupby(["branchId", col]).size().reset_index(name="records")
        many = c.groupby("branchId")[col].nunique()
        c = c[c["branchId"].isin(many[many > 1].index)]
        rows = []
        for b, g in c.groupby("branchId"):
            g = g.sort_values("records", ascending=False)
            rid = per_branch[b]
            rows.append({
                "branchId": b,
                "restaurant_ids": ", ".join(rid),
                "restaurant_names": " | ".join(str(name_of.get(x)) for x in rid),
                "values_count": len(g),
                "values (records)": " | ".join(
                    f"{v} ({label_map.get(v, 'Unmapped')}): {n}" for v, n in zip(g[col], g["records"])),
            })
        return pd.DataFrame(rows, columns=["branchId", "restaurant_ids", "restaurant_names",
                                           "values_count", "values (records)"])

    # ---------------- city ----------------
    city_attr = attr_table(raw, "shopCity")
    city_t, city_without = dist_table(pairs, city_attr, "shopCity", total_restaurants)
    city_t.insert(1, "shopCity_label", city_t["shopCity"].map(CITY_LABELS).fillna("Unmapped"))
    city_t = city_t.rename(columns={"shopCity": "shopCity_id"})

    multi_city_df = multi_value_branches("shopCity", CITY_LABELS)
    city_sum_branches = int(city_t["branches"].sum())
    city_unique_branches = int(city_attr["branchId"].nunique())
    city_unique_rest = total_restaurants - city_without
    total_row = {
        "shopCity_id": "TOTAL (unique)", "shopCity_label": "",
        "restaurants": city_unique_rest, "branches": city_unique_branches,
        "pct_of_restaurants": round(city_unique_rest / total_restaurants * 100, 2),
        "branches_per_restaurant": round(city_unique_branches / max(city_unique_rest, 1), 2),
    }
    city_out = pd.concat([city_t, pd.DataFrame([total_row])], ignore_index=True)

    section("5) RESTAURANTS BY CITY")
    out("  Restaurants: a restaurant is counted in every city where it has a branch,")
    out("  so the column sums to more than the unique total (last row).")
    show(city_out)
    kv("Sum of branches over cities", f"{city_sum_branches:,}   (unique branches with a city: {city_unique_branches:,})")
    if len(multi_city_df):
        out(f"\n  {len(multi_city_df)} branch(es) appear in more than one city in the JSONs "
            "(counted in each) - sheet Branches_In_Multiple_Cities:")
        show(multi_city_df, 10)

    # ---------------- area ----------------
    area_attr = attr_table(raw, "shopArea")
    area_t, area_without = dist_table(pairs, area_attr, "shopArea", total_restaurants)
    area_t.insert(1, "shopArea_label", area_t["shopArea"].map(area_map).fillna("Unmapped"))
    area_t["pct_of_branches"] = (area_t["branches"] / total_branches * 100).round(2)
    area_t = area_t.rename(columns={"shopArea": "shopArea_id"})
    area_by_branches = area_t.sort_values(["branches", "restaurants"], ascending=False).reset_index(drop=True)
    area_by_branches.insert(0, "rank", range(1, len(area_by_branches) + 1))
    area_density = area_t.copy()
    area_density.insert(0, "rank", range(1, len(area_density) + 1))
    highest = area_density.head(TOP_N)
    lowest = area_density.sort_values(["restaurants", "branches"]).head(TOP_N)
    dens = {
        "total_areas": int(len(area_t)),
        "avg_restaurants_per_area": round(float(area_t["restaurants"].mean()), 2),
        "median_restaurants_per_area": float(area_t["restaurants"].median()),
        "max_restaurants_in_area": int(area_t["restaurants"].max()),
        "min_restaurants_in_area": int(area_t["restaurants"].min()),
        "areas_with_only_1_restaurant": int((area_t["restaurants"] == 1).sum()),
    }
    labeled = int((area_t["shopArea_label"] != "Unmapped").sum())
    multi_area_df = multi_value_branches("shopArea", area_map)
    area_sum_branches = int(area_t["branches"].sum())
    area_unique_branches = int(area_attr["branchId"].nunique())

    section("6) AREAS (BRANCHES & DENSITY)")
    kv("Total areas", dens["total_areas"])
    kv("Avg / median restaurants per area", f"{dens['avg_restaurants_per_area']} / {dens['median_restaurants_per_area']}")
    kv("Areas with only 1 restaurant", dens["areas_with_only_1_restaurant"])
    kv("Area labels source", f"{area_src}  ({labeled}/{dens['total_areas']} areas labelled)")
    kv("Sum of branches over areas", f"{area_sum_branches:,}   (unique branches with an area: {area_unique_branches:,})")
    if len(multi_area_df):
        out(f"  {len(multi_area_df)} branch(es) appear in more than one area in the JSONs "
            "(counted in each) - sheet Branches_In_Multiple_Areas")
    out(f"\n  Top {TOP_N} areas by restaurants (highest density):")
    show(highest[["rank", "shopArea_id", "shopArea_label", "restaurants", "branches"]])
    out(f"\n  Bottom {TOP_N} areas by restaurants (lowest density, ties are arbitrary):")
    show(lowest[["shopArea_id", "shopArea_label", "restaurants", "branches"]])

    # ---------------- activity ----------------
    cu_attr = attr_table(raw, "cuisineString", comma_split=True, casefold=True)
    cu_t, cu_without = dist_table(pairs, cu_attr, "cuisineString", total_restaurants)
    cu_t["cuisineString"] = cu_t["cuisineString"].str.title()
    cu_t = cu_t.rename(columns={"cuisineString": "cuisine"})

    vt_attr = attr_table(raw, "verticalType")
    vt_t, vt_without = dist_table(pairs, vt_attr, "verticalType", total_restaurants)

    section("7) RESTAURANTS BY ACTIVITY")
    out("  (a restaurant can have several cuisines, so % sums to more than 100)")
    out(f"\n  By cuisine - {len(cu_t)} cuisines, {cu_without} restaurants without cuisine. Top {TOP_N}:")
    show(cu_t[["cuisine", "restaurants", "pct_of_restaurants", "branches"]], TOP_N)
    out(f"\n  By verticalType - {len(vt_t)} values, {vt_without} restaurants without verticalType:")
    show(vt_t[["verticalType", "restaurants", "pct_of_restaurants", "branches"]])

    # ---------------- save ----------------
    overview = pd.DataFrame([
        ("Totals", "total_restaurants", total_restaurants),
        *([("Totals", "expected_restaurants", EXPECTED_RESTAURANTS)] if EXPECTED_RESTAURANTS else []),
        ("Totals", "total_branches_unique", total_branches),
        ("Totals", "sum_of_branches_listed_per_restaurant", sum_branch_counts),
        ("Branches per restaurant", "average_listed", avg_listed),
        ("Branches per restaurant", "average_using_unique_branches", avg_unique),
        ("Branches per restaurant", "median", median_b),
        ("Branches per restaurant", "max", int(bpr.max())),
        *[("Restaurants vs branches", k, v) for k, v in ratio.items()],
        ("Cities", "total_cities", int(len(city_t))),
        *[("Areas", k, v) for k, v in dens.items()],
        ("Activity", "total_cuisines", int(len(cu_t))),
        ("Activity", "restaurants_without_cuisine", cu_without),
        ("Activity", "restaurants_without_verticalType", vt_without),
    ], columns=["section", "metric", "value"])

    dq = pd.DataFrame([
        ("json_files", n_files),
        ("json_records", total_records),
        ("records_ignored_branch_not_in_validation_file", dropped_records),
        ("distinct_branchIds_ignored", len(dropped_branch_ids)),
        ("validation_branches_missing_from_json", len(missing_in_raw)),
        ("shared_branches_validation_file", len(shared_df)),
        ("shared_branches_json", len(shared_json_df)),
        *[(f"shared_branches_json: {k}", int(v)) for k, v in shared_json_df["status"].value_counts().items()],
        ("duplicate_restaurant_ids_in_validation_sheet", len(dup_ids)),
        ("sum_of_branches_over_cities", city_sum_branches),
        ("branches_in_more_than_one_city", len(multi_city_df)),
        ("sum_of_branches_over_areas", area_sum_branches),
        ("branches_in_more_than_one_area", len(multi_area_df)),
        ("restaurants_without_city", city_without),
        ("restaurants_without_area", area_without),
    ], columns=["check", "value"])

    summary_sheets = {
        "Overview": overview,
        "Branch_Distribution": dist_df,
        "Area_Density_All": area_density,
        "Highest_Density": highest,
        "Lowest_Density": lowest,
        "Data_Quality": dq,
    }
    if len(shared_df):
        summary_sheets["Shared_Branches_Unique"] = shared_df
    if len(shared_json_df):
        summary_sheets["Shared_Branches_JSON"] = shared_json_df
    if len(multi_city_df):
        summary_sheets["Branches_In_Multiple_Cities"] = multi_city_df
    if len(multi_area_df):
        summary_sheets["Branches_In_Multiple_Areas"] = multi_area_df
    if missing_in_raw:
        summary_sheets["Missing_In_JSON"] = pd.DataFrame({
            "branchId": missing_in_raw,
            "restaurant_id": [per_branch[b][0] for b in missing_in_raw],
            "restaurant": [name_of.get(per_branch[b][0]) for b in missing_in_raw],
        })

    files = [
        write_excel("market_structure_summary.xlsx", summary_sheets),
        write_excel("branches_per_restaurant.xlsx", {"Branches_per_Restaurant": all_rest_t}),
        write_excel("multi_branch_restaurants.xlsx", {"Multi_Branch_Restaurants": multi_rest_t}),
        write_excel("restaurants_by_city.xlsx", {
            "By_City": city_out,
            **({"Branches_In_Multiple_Cities": multi_city_df} if len(multi_city_df) else {})}),
        write_excel("branches_by_area.xlsx", {
            "By_Area": area_by_branches,
            **({"Branches_In_Multiple_Areas": multi_area_df} if len(multi_area_df) else {})}),
        write_excel("restaurants_by_activity.xlsx", {"By_Cuisine": cu_t, "By_VerticalType": vt_t}),
    ]

    section("8) FILES SAVED")
    for f in files:
        out(f"  {f}")
    out("  market_structure_report.txt")
    if EXPECTED_RESTAURANTS and total_restaurants != EXPECTED_RESTAURANTS:
        out(f"\n[WARNING] restaurants={total_restaurants} but expected {EXPECTED_RESTAURANTS}.")

    with open(os.path.join(OUT_DIR, "market_structure_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))


if __name__ == "__main__":
    sys.exit(main())