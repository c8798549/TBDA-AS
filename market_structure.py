"""
market_structure.py
-------------------
Market / Restaurant Structure analysis.

المصدر:
  1) ملف الـ manual validation (sheet "unique"): كل صف = مطعم واحد (3979)،
     وعمود branchId فيه list بكل فروع المطعم.  <- ده المرجع لتعريف المطعم وفروعه.
  2) الـ 119 JSON على R2: بنستخدمهم بس عشان نجيب shopCity / shopArea / cuisineString
     للفروع (بالـ branchId). أي record فرعه مش في الملف بيتم تجاهله.

Outputs (OUT_DIR):
  market_structure_summary.json, branches_per_restaurant.json,
  multi_branch_restaurants.json, branch_count_distribution.json,
  restaurants_by_city.json, branches_by_area.json, area_density.json,
  restaurants_by_cuisine.json, data_quality.json
"""

import io
import json
import os
import re
import sys

import boto3
import pandas as pd

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

MAPPING_R2_KEY = os.environ.get("MAPPING_R2_KEY", "")  # لو الملف على R2
MAPPING_PATH = os.environ.get("MAPPING_PATH", "data/restaurants_validated.xlsx")
MAPPING_SHEET = os.environ.get("MAPPING_SHEET", "")  # فاضي = أول sheet اسمه فيه "unique"
MAPPING_ID_COL = os.environ.get("MAPPING_ID_COL", "id")
MAPPING_NAME_COL = os.environ.get("MAPPING_NAME_COL", "name")
MAPPING_BRANCH_COL = os.environ.get("MAPPING_BRANCH_COL", "branchId")

EXPECTED_RESTAURANTS = int(os.environ.get("EXPECTED_RESTAURANTS", "3979"))
TOP_N = int(os.environ.get("TOP_N", "10"))
OUT_DIR = os.environ.get("OUT_DIR", "market_structure_output")

RAW_COLS = ["branchId", "shopCity", "shopArea", "cuisineString"]
os.makedirs(OUT_DIR, exist_ok=True)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)


def save(name, obj):
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {name}")


def parse_ids(v):
    """'[720193, 682706]' أو '720193' أو list -> list of digit-strings."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    return re.findall(r"\d+", str(v))


def to_list(v):
    """shopArea / shopCity -> list of strings."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip() not in ("", "None")]
    s = str(v).strip()
    return [] if s in ("", "None", "nan") else [s]


# ----------------------------------------------------------------------------
# 1) Manual validation file (sheet unique)
# ----------------------------------------------------------------------------
def load_mapping():
    if MAPPING_R2_KEY:
        src = io.BytesIO(s3.get_object(Bucket=R2_BUCKET, Key=MAPPING_R2_KEY)["Body"].read())
        name = MAPPING_R2_KEY
    else:
        src, name = MAPPING_PATH, MAPPING_PATH

    if name.lower().endswith(".csv"):
        m = pd.read_csv(src, dtype=str)
        sheet_used = "csv"
    else:
        xl = pd.ExcelFile(src)
        if MAPPING_SHEET:
            sheet_used = MAPPING_SHEET
        else:
            cands = [s for s in xl.sheet_names if "unique" in s.lower()]
            sheet_used = cands[0] if cands else xl.sheet_names[0]
        m = xl.parse(sheet_used, dtype=str)
    print(f"Mapping sheet: {sheet_used} | rows: {len(m):,}")

    m = m[[MAPPING_ID_COL, MAPPING_NAME_COL, MAPPING_BRANCH_COL]].copy()
    m[MAPPING_ID_COL] = m[MAPPING_ID_COL].astype(str).str.strip()
    m["branch_list"] = m[MAPPING_BRANCH_COL].apply(parse_ids)

    dup_ids = m[m.duplicated(MAPPING_ID_COL, keep=False)][MAPPING_ID_COL].unique().tolist()
    m = m.drop_duplicates(MAPPING_ID_COL, keep="first")

    rest_name = dict(zip(m[MAPPING_ID_COL], m[MAPPING_NAME_COL]))
    rest_branches = {
        rid: sorted(set(bl)) for rid, bl in zip(m[MAPPING_ID_COL], m["branch_list"])
    }

    # branchId -> restaurant (لو فرع ظهر تحت أكتر من مطعم ناخد الأول ونسجل)
    branch_to_rest, conflicts = {}, {}
    for rid, bl in rest_branches.items():
        for b in bl:
            if b in branch_to_rest and branch_to_rest[b] != rid:
                conflicts.setdefault(b, {branch_to_rest[b]}).add(rid)
            else:
                branch_to_rest[b] = rid
    return rest_name, rest_branches, branch_to_rest, conflicts, dup_ids


# ----------------------------------------------------------------------------
# 2) Raw JSON files
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
# 3) Main
# ----------------------------------------------------------------------------
def main():
    rest_name, rest_branches, branch_to_rest, conflicts, dup_ids = load_mapping()
    raw, n_files = load_raw()
    total_records = len(raw)

    # ---------------- A) totals (من ملف الـ manual validation) ----------------
    total_restaurants = len(rest_branches)
    all_mapping_branches = set(branch_to_rest)
    total_branches = len(all_mapping_branches)

    bpr = pd.Series({rid: len(bl) for rid, bl in rest_branches.items()}).sort_values(
        ascending=False
    )
    restaurants_zero_branches = int((bpr == 0).sum())

    # ---------------- ربط الـ raw بالمطاعم (بالـ branchId بس) ----------------
    raw["branchId"] = raw["branchId"].astype(str).str.strip()
    in_map = raw["branchId"].isin(all_mapping_branches)
    dropped_records = int((~in_map).sum())
    dropped_branch_ids = sorted(raw.loc[~in_map, "branchId"].unique().tolist())
    df = raw[in_map].copy()
    df["restaurant_id"] = df["branchId"].map(branch_to_rest)
    mapping_branches_not_in_raw = sorted(all_mapping_branches - set(df["branchId"].unique()))

    print(f"\nrecords: {total_records:,} | files: {n_files}")
    print(f"records ignored (branchId not in validation file): {dropped_records:,} "
          f"({len(dropped_branch_ids):,} distinct branchIds)")
    print(f"branches in validation file but not found in raw: {len(mapping_branches_not_in_raw):,}")
    if mapping_branches_not_in_raw:
        print("Branches in validation file but missing from the JSON files "
              "(counted in branch totals, but have no city/area/cuisine):")
        for b in mapping_branches_not_in_raw:
            rid = branch_to_rest[b]
            print(f"  branchId {b} -> {rest_name.get(rid)} [restaurant id {rid}]")
    print(f"branches linked to >1 restaurant in validation file: {len(conflicts)}")

    # ---------------- B) branches per restaurant ----------------
    def label(rid):
        return {"restaurant_id": rid, "restaurant": rest_name.get(rid)}

    branches_per_restaurant = [{**label(k), "branches": int(v)} for k, v in bpr.items()]
    multi = bpr[bpr > 1]
    single = bpr[bpr == 1]
    avg_branches = round(float(bpr.mean()), 3)
    median_branches = float(bpr.median())

    bins = [(1, 1), (2, 2), (3, 3), (4, 5), (6, 10), (11, 20), (21, 50), (51, 10**9)]
    dist = []
    for lo, hi in bins:
        n = int(((bpr >= lo) & (bpr <= hi)).sum())
        lab = f"{lo}" if lo == hi else (f"{lo}+" if hi == 10**9 else f"{lo}-{hi}")
        dist.append({"branches_range": lab, "restaurants": n,
                     "pct_of_restaurants": round(n / total_restaurants * 100, 2)})

    # ---------------- C) restaurants vs branches ----------------
    branches_of_multi = int(multi.sum())
    ratio = {
        "restaurants_to_branches_pct": round(total_restaurants / total_branches * 100, 2),
        "branches_per_restaurant": avg_branches,
        "single_branch_restaurants": int(len(single)),
        "single_branch_restaurants_pct": round(len(single) / total_restaurants * 100, 2),
        "multi_branch_restaurants": int(len(multi)),
        "multi_branch_restaurants_pct": round(len(multi) / total_restaurants * 100, 2),
        "branches_owned_by_multi_branch_restaurants": branches_of_multi,
        "branches_owned_by_multi_branch_restaurants_pct": round(
            branches_of_multi / int(bpr.sum()) * 100, 2),
    }

    # ---------------- D) city + area (الفرع بيتعد في كل منطقة/مدينة ظاهر فيها) ----------------
    def geo_table(col):
        g = df[["restaurant_id", "branchId", col]].copy()
        g[col] = g[col].apply(to_list)
        g = g.explode(col).dropna(subset=[col]).drop_duplicates()
        out = (g.groupby(col)
                 .agg(restaurants=("restaurant_id", "nunique"), branches=("branchId", "nunique"))
                 .reset_index())
        out["branches_per_restaurant"] = (out["branches"] / out["restaurants"]).round(2)
        return out

    city_t = geo_table("shopCity").rename(columns={"shopCity": "city"})
    city_t["pct_of_restaurants"] = (city_t["restaurants"] / total_restaurants * 100).round(2)
    city_t = city_t.sort_values("restaurants", ascending=False)

    area_t = geo_table("shopArea").rename(columns={"shopArea": "area"})
    area_by_branches = area_t.sort_values("branches", ascending=False)
    area_t["restaurant_density_share_pct"] = (
        area_t["restaurants"] / area_t["restaurants"].sum() * 100).round(3)
    area_density = area_t.sort_values(["restaurants", "branches"], ascending=False).reset_index(drop=True)
    highest = area_density.head(TOP_N)
    lowest = area_density.sort_values(["restaurants", "branches"]).head(TOP_N)
    dens_stats = {
        "total_areas": int(len(area_t)),
        "avg_restaurants_per_area": round(float(area_density["restaurants"].mean()), 2),
        "median_restaurants_per_area": float(area_density["restaurants"].median()),
        "max_restaurants_in_area": int(area_density["restaurants"].max()),
        "min_restaurants_in_area": int(area_density["restaurants"].min()),
        "areas_with_only_1_restaurant": int((area_density["restaurants"] == 1).sum()),
    }

    # ---------------- E) cuisine ----------------
    cz = df[["restaurant_id", "branchId", "cuisineString"]].copy()
    cz["cuisine"] = cz["cuisineString"].fillna("").astype(str).str.split(",")
    cz = cz.explode("cuisine")
    cz["cuisine"] = cz["cuisine"].str.strip().str.casefold()
    cz = cz[~cz["cuisine"].isin(["", "none", "nan"])]
    cz = cz[["restaurant_id", "branchId", "cuisine"]].drop_duplicates()
    cuisine_t = (cz.groupby("cuisine")
                   .agg(restaurants=("restaurant_id", "nunique"), branches=("branchId", "nunique"))
                   .reset_index().sort_values("restaurants", ascending=False))
    cuisine_t["pct_of_restaurants"] = (cuisine_t["restaurants"] / total_restaurants * 100).round(2)
    cuisine_t["cuisine"] = cuisine_t["cuisine"].str.title()
    restaurants_without_cuisine = int(total_restaurants - cz["restaurant_id"].nunique())

    # ---------------- F) save ----------------
    summary = {
        "source": {
            "r2_prefix": R2_PREFIX, "files": n_files, "total_records": total_records,
            "records_ignored_not_in_validation_file": dropped_records,
        },
        "totals": {
            "total_restaurants": total_restaurants,
            "expected_restaurants": EXPECTED_RESTAURANTS,
            "matches_expected": total_restaurants == EXPECTED_RESTAURANTS,
            "total_branches": total_branches,
        },
        "branches_per_restaurant_stats": {
            "average": avg_branches, "median": median_branches, "max": int(bpr.max()),
            "top_10_restaurants_by_branches": branches_per_restaurant[:10],
        },
        "multi_branch": {"count": int(len(multi)),
                         "pct_of_restaurants": ratio["multi_branch_restaurants_pct"]},
        "restaurants_vs_branches": ratio,
        "branch_count_distribution": dist,
        "cities": {"total_cities": int(len(city_t)), "table": city_t.to_dict("records")},
        "areas": {**dens_stats,
                  "highest_density": highest.to_dict("records"),
                  "lowest_density": lowest.to_dict("records"),
                  "top_areas_by_branches": area_by_branches.head(TOP_N).to_dict("records")},
        "cuisines": {"total_cuisines": int(len(cuisine_t)),
                     "restaurants_without_cuisine": restaurants_without_cuisine,
                     "top_10": cuisine_t.head(10).to_dict("records")},
    }
    quality = {
        "duplicate_restaurant_ids_in_validation_sheet": dup_ids[:100],
        "restaurants_with_zero_branches": restaurants_zero_branches,
        "branches_linked_to_more_than_one_restaurant_count": len(conflicts),
        "branches_linked_to_more_than_one_restaurant_sample": {
            b: sorted(r) for b, r in list(conflicts.items())[:50]},
        "raw_records_ignored": dropped_records,
        "raw_branch_ids_ignored_count": len(dropped_branch_ids),
        "raw_branch_ids_ignored_sample": dropped_branch_ids[:50],
        "validation_branches_not_found_in_raw_count": len(mapping_branches_not_in_raw),
        "validation_branches_not_found_in_raw": [
            {"branchId": b, "restaurant_id": branch_to_rest[b],
             "restaurant": rest_name.get(branch_to_rest[b])}
            for b in mapping_branches_not_in_raw
        ],
    }

    print("\nSaving outputs:")
    save("market_structure_summary.json", summary)
    save("branches_per_restaurant.json", branches_per_restaurant)
    save("multi_branch_restaurants.json", [{**label(k), "branches": int(v)} for k, v in multi.items()])
    save("branch_count_distribution.json", dist)
    save("restaurants_by_city.json", city_t.to_dict("records"))
    save("branches_by_area.json", area_by_branches.to_dict("records"))
    save("area_density.json", {"stats": dens_stats, "highest": highest.to_dict("records"),
                               "lowest": lowest.to_dict("records"),
                               "all_areas": area_density.to_dict("records")})
    save("restaurants_by_cuisine.json", cuisine_t.to_dict("records"))
    save("data_quality.json", quality)

    # ---------------- logs ----------------
    print("\n===== MARKET / RESTAURANT STRUCTURE =====")
    print(f"Total restaurants           : {total_restaurants:,} (expected {EXPECTED_RESTAURANTS:,})")
    print(f"Total branches              : {total_branches:,}")
    print(f"Avg branches / restaurant   : {avg_branches}  (median {median_branches})")
    print(f"Multi-branch restaurants    : {len(multi):,} ({ratio['multi_branch_restaurants_pct']}%)")
    print(f"Single-branch restaurants   : {len(single):,} ({ratio['single_branch_restaurants_pct']}%)")
    print(f"Restaurants/Branches        : {ratio['restaurants_to_branches_pct']}%")
    print("\nTop 10 restaurants by branches:")
    for r in branches_per_restaurant[:10]:
        print(f"  {r['restaurant']} [{r['restaurant_id']}]: {r['branches']}")
    print("\nBranch-count distribution:")
    for d in dist:
        print(f"  {d['branches_range']:>6}: {d['restaurants']:,} ({d['pct_of_restaurants']}%)")
    print("\nRestaurants per city:")
    for r in city_t.to_dict("records"):
        print(f"  city {r['city']}: {r['restaurants']:,} restaurants / {r['branches']:,} branches")
    print(f"\nAreas: {dens_stats['total_areas']} | avg restaurants/area {dens_stats['avg_restaurants_per_area']}")
    print("Highest-density areas:")
    for r in highest.to_dict("records"):
        print(f"  area {r['area']}: {r['restaurants']} restaurants / {r['branches']} branches")
    print("Lowest-density areas:")
    for r in lowest.to_dict("records"):
        print(f"  area {r['area']}: {r['restaurants']} restaurants / {r['branches']} branches")
    print("\nTop 10 cuisines (restaurants):")
    for r in cuisine_t.head(10).to_dict("records"):
        print(f"  {r['cuisine']}: {r['restaurants']:,} ({r['pct_of_restaurants']}%)")

    if total_restaurants != EXPECTED_RESTAURANTS:
        print(f"\n[WARNING] restaurants={total_restaurants} but expected {EXPECTED_RESTAURANTS}. "
              "Check data_quality.json (duplicate ids in the unique sheet).")


if __name__ == "__main__":
    sys.exit(main())