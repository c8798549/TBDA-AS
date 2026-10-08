import os
import re
import json
import unicodedata
from collections import defaultdict, Counter
from itertools import combinations

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

# ---------------- output files ----------------
SUMMARY_FILE = "cuisine_insights.json"
COUNTS_FILE = "cuisine_insights_cuisine_counts.json"
AREA_MIX_FILE = "cuisine_insights_area_mix.json"
CONCENTRATION_FILE = "cuisine_insights_concentration.json"
DOMINANCE_FILE = "cuisine_insights_dominance.json"
MULTI_CUISINE_FILE = "cuisine_insights_multi_cuisine_restaurants.json"
COMBINATIONS_FILE = "cuisine_insights_combinations.json"
SPREAD_FILE = "cuisine_insights_geographic_spread.json"

# ---------------- thresholds (عدّليهم من هنا) ----------------
MIN_AREA_RESTAURANTS = 20        # أقل عدد مطاعم لمنطقة تدخل تحليل #6 و #7 و #10
MIN_CUISINE_IN_AREA = 5          # أقل عدد مطاعم من النوع في المنطقة لـ #6
LIFT_MIN = 1.5                   # أقل lift لـ #6
DOMINANCE_MIN_SHARE = 0.40       # نسبة الهيمنة في #7
FEW_AREAS_MAX = 3                # أقصى عدد مناطق لـ "قليلة" في #11
LOCAL_MIN_RESTAURANTS = 3        # أقل مطاعم لقايمة الأنواع المحلية في #11
SHARED_MIN_PCT_OF_AREAS = 0.80   # نسبة المناطق لـ "مشترك" في #10

MAX_PER_AREA_CONCENTRATION = 10  # أقصى عدد أنواع بتتحفظ لكل منطقة في #6
MAX_AREAS_PER_CUISINE = 10       # أقصى عدد مناطق بتتحفظ لكل نوع في #6
MAX_COMBINATIONS_SAVED = 2000     # أقصى عدد أزواج/مجموعات بتتحفظ في #9

SAMPLES = 5                      # عدد الأمثلة المطبوعة
TOP_N_PRINT = 10                 # عدد الترتيبات المطبوعة
SHARED_PRINT_MAX = 30            # أقصى عدد أنواع مشتركة بتتطبع

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]

APOSTROPHES = re.compile(r"[\'`´‘’ʼ′ʻ]")


# ============================================================
# HELPERS
# ============================================================

def load_records(key):
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    data = json.loads(body)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "records", "items", "results"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return []


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def norm_code(value):
    """يوحّد أكواد المدينة/المنطقة: 1 و 1.0 و "1" كلهم "1"."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:          # NaN
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text or None


def sort_key(value):
    """ترتيب الـ ids رقميًا لو أرقام، وإلا نصيًا."""
    value = str(value)
    if value.isdigit():
        return (0, int(value), value)
    return (1, 0, value)


def first_segment(name):
    """الجزء الأول من الاسم قبل أول فاصلة (دا هو اسم المطعم)."""
    return name.split(",")[0].strip()


def brand_key(name):
    """
    مفتاح المقارنة:
    - الجزء الأول قبل أول فاصلة
    - توحيد unicode
    - حذف كل أنواع الـ apostrophe
    - lowercase + توحيد المسافات
    """
    seg = unicodedata.normalize("NFKC", first_segment(name))
    seg = APOSTROPHES.sub("", seg)
    seg = re.sub(r"\s+", " ", seg).strip().lower()
    return seg


def top_name(counter):
    """أكتر قيمة اتكررت (None لو مفيش)."""
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def safe_div(a, b, digits=2):
    return round(a / b, digits) if b else 0


def short_list(values, n=5):
    values = list(values)
    if len(values) <= n:
        return str(values)
    return str(values[:n])[:-1] + f", ... +{len(values) - n} more]"


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def print_block(title, total, lines, filename=None):
    """عنوان + عدد + samples + مكان الملف الكامل."""
    print(f"\n{title} ({total})")

    for line in lines:
        print(f"  {line}")

    if total > len(lines):
        print(f"  ... and {total - len(lines)} more")

    if filename:
        print(f"  -> full details: {filename}")


# ============================================================
# MAIN
# ============================================================

def main():
    # ----------------------------------------------------
    # list files
    # ----------------------------------------------------
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                files.append(obj["Key"])
    files.sort()
    print(f"Files found: {len(files)}")

    # ----------------------------------------------------
    # containers
    # ----------------------------------------------------
    names = set()
    id_to_names = defaultdict(Counter)
    name_to_ids = defaultdict(set)

    branch_ids = set()
    id_to_branch_ids = defaultdict(set)

    branch_area = defaultdict(set)          # branchId -> كل المناطق
    branch_city = defaultdict(set)          # branchId -> كل المدن
    branch_loc_triples = set()              # (branchId, area, city)

    id_to_cuisines = defaultdict(set)       # id -> أنواع (lowercase)
    cuisine_forms = defaultdict(Counter)    # شكل الكتابة لكل نوع

    total_records = 0

    # ----------------------------------------------------
    # process files
    # ----------------------------------------------------
    for i, key in enumerate(files, start=1):
        records = load_records(key)
        total_records += len(records)

        for r in records:
            _id = clean(r.get("id"))
            name = clean(r.get("name"))
            b_id = clean(r.get("branchId"))
            cuisine_str = clean(r.get("cuisineString"))
            city = norm_code(r.get("shopCity"))
            area = norm_code(r.get("shopArea"))

            if name:
                names.add(name)
            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)

            if b_id:
                branch_ids.add(b_id)
            if _id and b_id:
                id_to_branch_ids[_id].add(b_id)

            # الموقع: الفرع بيتسجل في كل منطقة/مدينة ظهر فيها
            if b_id and area:
                branch_area[b_id].add(area)
            if b_id and city:
                branch_city[b_id].add(city)
            if b_id and area and city:
                branch_loc_triples.add((b_id, area, city))

            # أنواع النشاط (نقسم على الفاصلة)
            if _id and cuisine_str:
                for part in cuisine_str.split(","):
                    p = re.sub(r"\s+", " ", part).strip()
                    if not p:
                        continue
                    ck = p.lower()
                    id_to_cuisines[_id].add(ck)
                    cuisine_forms[ck][p] += 1

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    def cname(c):
        """اسم العرض للنوع."""
        return top_name(cuisine_forms.get(c)) or c

    # ----------------------------------------------------
    # group restaurants
    #   القاعدة 1: الجزء الأول من الاسم (قبل أول فاصلة)
    #   القاعدة 2: أي أسماء ظهرت مع نفس الـ id تتدمج (id = المطعم)
    # ----------------------------------------------------
    brand_to_ids = defaultdict(set)
    brand_variants = defaultdict(Counter)

    for name in sorted(names):
        k = brand_key(name)
        if not k:
            continue
        brand_variants[k][first_segment(name)] += 1
        brand_to_ids[k].update(name_to_ids[name])

    parent = {k: k for k in brand_variants}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for rid, counter in id_to_names.items():
        keys = [brand_key(n) for n in counter]
        keys = [k for k in keys if k]
        for other in keys[1:]:
            union(keys[0], other)

    components = defaultdict(list)
    for k in brand_variants:
        components[find(k)].append(k)

    # كل مطعم بعد التجميع: فروعه وأنواعه ومناطقه ومدنه
    restaurants = {}

    for root in sorted(components):
        keys = sorted(components[root])

        comp_ids = set()
        variants = Counter()
        for k in keys:
            comp_ids |= brand_to_ids[k]
            variants.update(brand_variants[k])

        display = sorted(
            variants.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]
        if display in restaurants:
            display = f"{display} ({root})"

        bids, cuisines, areas, cities = set(), set(), set(), set()
        for rid in comp_ids:
            bids.update(id_to_branch_ids.get(rid, ()))
            cuisines.update(id_to_cuisines.get(rid, ()))
        for bid in bids:
            areas.update(branch_area.get(bid, ()))
            cities.update(branch_city.get(bid, ()))

        restaurants[display] = {
            "rep_id": min(comp_ids, key=sort_key) if comp_ids else None,
            "ids": len(comp_ids),
            "branches": bids,
            "cuisines": cuisines,
            "areas": areas,
            "cities": cities,
        }

    total_restaurants = len(restaurants)
    total_branches = len(branch_ids)

    if total_restaurants == 0:
        print("No restaurants found.")
        return

    # ----------------------------------------------------
    # area -> city (من الثلاثية: فرع + منطقة + مدينة)
    # ----------------------------------------------------
    area_to_cities = defaultdict(Counter)
    for (_bid, a, c) in branch_loc_triples:
        area_to_cities[a][c] += 1

    def area_city(area):
        return top_name(area_to_cities.get(area))

    # ----------------------------------------------------
    # aggregates: areas
    # ----------------------------------------------------
    area_restaurants = Counter()
    area_cuisine = defaultdict(Counter)     # area -> Counter(cuisine -> restaurants)

    for r in restaurants.values():
        for a in r["areas"]:
            area_restaurants[a] += 1
            for c in r["cuisines"]:
                area_cuisine[a][c] += 1

    area_branches = Counter()
    for bid in branch_ids:
        for a in branch_area.get(bid, ()):
            area_branches[a] += 1

    areas_sorted = sorted(
        area_restaurants, key=lambda a: (-area_restaurants[a], sort_key(a))
    )
    total_areas = len(areas_sorted)

    eligible = [
        a for a in areas_sorted
        if area_restaurants[a] >= MIN_AREA_RESTAURANTS
    ]

    # ----------------------------------------------------
    # aggregates: cuisines
    # ----------------------------------------------------
    cuisine_restaurants = Counter()
    cuisine_branch_sets = defaultdict(set)
    cuisine_cities = defaultdict(set)

    for r in restaurants.values():
        for c in r["cuisines"]:
            cuisine_restaurants[c] += 1
            cuisine_branch_sets[c].update(r["branches"])
            cuisine_cities[c].update(r["cities"])

    cuisine_areas = defaultdict(set)
    for a, counter in area_cuisine.items():
        for c in counter:
            cuisine_areas[c].add(a)

    overall_share = {
        c: n / total_restaurants for c, n in cuisine_restaurants.items()
    }

    restaurants_with_cuisine = sum(
        1 for r in restaurants.values() if r["cuisines"]
    )
    restaurants_without_cuisine = total_restaurants - restaurants_with_cuisine

    # ====================================================
    # #1 #2 #4 #5: restaurants / branches per cuisine
    # ====================================================
    cuisine_stats = []
    for c, n in cuisine_restaurants.items():
        b = len(cuisine_branch_sets[c])
        cuisine_stats.append({
            "cuisine": cname(c),
            "restaurants": n,
            "pct_of_restaurants": safe_div(n * 100, total_restaurants),
            "branches": b,
            "pct_of_branches": safe_div(b * 100, total_branches),
            "avg_branches_per_restaurant": safe_div(b, n),
            "areas": len(cuisine_areas.get(c, ())),
            "cities": len(cuisine_cities.get(c, ())),
        })

    cuisine_stats.sort(
        key=lambda x: (-x["restaurants"], x["cuisine"].lower())
    )

    most_widespread = cuisine_stats[:TOP_N_PRINT]
    least_widespread = sorted(
        cuisine_stats,
        key=lambda x: (x["restaurants"], x["cuisine"].lower()),
    )[:TOP_N_PRINT]
    cuisines_single_restaurant = sum(
        1 for x in cuisine_stats if x["restaurants"] == 1
    )

    counts_file = {
        "total_restaurants": total_restaurants,
        "total_branches": total_branches,
        "unique_cuisines": len(cuisine_stats),
        "note": (
            "A restaurant is counted under each of its cuisines, and a "
            "branch is counted once under each cuisine of its restaurant, "
            "so the totals are above the real numbers of restaurants and "
            "branches."
        ),
        "cuisines": cuisine_stats,
    }

    # ====================================================
    # #3: cuisine mix per area
    # ====================================================
    area_mix = []
    for a in areas_sorted:
        n_area = area_restaurants[a]
        mix = [
            {
                "cuisine": cname(c),
                "restaurants": k,
                "pct_of_area_restaurants": safe_div(k * 100, n_area),
            }
            for c, k in sorted(
                area_cuisine[a].items(),
                key=lambda x: (-x[1], cname(x[0]).lower()),
            )
        ]
        area_mix.append({
            "area": a,
            "city": area_city(a),
            "restaurants": n_area,
            "branches": area_branches.get(a, 0),
            "distinct_cuisines": len(mix),
            "cuisines": mix,
        })

    area_mix_file = {
        "total_areas": total_areas,
        "note": (
            "Percentages are from the restaurants of the area. A restaurant "
            "with several cuisines is counted under each, so they can add "
            "up to more than 100."
        ),
        "areas": area_mix,
    }

    # ====================================================
    # #6: concentration (lift)
    # ====================================================
    candidates = []
    for a in eligible:
        n_area = area_restaurants[a]
        for c, k in area_cuisine[a].items():
            if k < MIN_CUISINE_IN_AREA:
                continue
            share = k / n_area
            candidates.append({
                "area": a,
                "c": c,
                "count": k,
                "share": share,
                "lift": share / overall_share[c],
            })

    qualified = [x for x in candidates if x["lift"] >= LIFT_MIN]

    by_area_map = defaultdict(list)
    by_cuisine_map = defaultdict(list)
    for x in qualified:
        by_area_map[x["area"]].append(x)
        by_cuisine_map[x["c"]].append(x)

    conc_by_area = []
    for a, items in by_area_map.items():
        items.sort(key=lambda x: (-x["lift"], cname(x["c"]).lower()))
        conc_by_area.append({
            "area": a,
            "city": area_city(a),
            "restaurants": area_restaurants[a],
            "top_cuisines": [
                {
                    "cuisine": cname(x["c"]),
                    "restaurants_in_area": x["count"],
                    "share_in_area_pct": round(x["share"] * 100, 2),
                    "overall_share_pct": round(
                        overall_share[x["c"]] * 100, 2
                    ),
                    "lift": round(x["lift"], 2),
                }
                for x in items[:MAX_PER_AREA_CONCENTRATION]
            ],
        })
    conc_by_area.sort(
        key=lambda e: (-e["top_cuisines"][0]["lift"], sort_key(e["area"]))
    )

    conc_by_cuisine = []
    for c, items in by_cuisine_map.items():
        items.sort(key=lambda x: (-x["lift"], sort_key(x["area"])))
        conc_by_cuisine.append({
            "cuisine": cname(c),
            "overall_restaurants": cuisine_restaurants[c],
            "overall_share_pct": round(overall_share[c] * 100, 2),
            "top_areas": [
                {
                    "area": x["area"],
                    "city": area_city(x["area"]),
                    "restaurants_in_area": x["count"],
                    "share_in_area_pct": round(x["share"] * 100, 2),
                    "lift": round(x["lift"], 2),
                }
                for x in items[:MAX_AREAS_PER_CUISINE]
            ],
        })
    conc_by_cuisine.sort(
        key=lambda e: (-e["top_areas"][0]["lift"], e["cuisine"].lower())
    )

    concentration_file = {
        "thresholds": {
            "min_area_restaurants": MIN_AREA_RESTAURANTS,
            "min_cuisine_restaurants_in_area": MIN_CUISINE_IN_AREA,
            "min_lift": LIFT_MIN,
        },
        "note": (
            "lift = share of the cuisine in the area / share of the cuisine "
            "in all restaurants. 2.0 means twice the usual."
        ),
        "eligible_areas": len(eligible),
        "pairs_checked": len(candidates),
        "pairs_qualified": len(qualified),
        "by_area": conc_by_area,
        "by_cuisine": conc_by_cuisine,
    }

    # ====================================================
    # #7: dominance
    # ====================================================
    dominance_all = []
    for a in eligible:
        n_area = area_restaurants[a]
        items = sorted(
            area_cuisine[a].items(),
            key=lambda x: (-x[1], cname(x[0]).lower()),
        )
        if not items:
            continue

        top_c, top_k = items[0]
        share = top_k / n_area
        second = items[1] if len(items) > 1 else None

        dominance_all.append({
            "area": a,
            "city": area_city(a),
            "restaurants": n_area,
            "dominant_cuisine": cname(top_c),
            "dominant_restaurants": top_k,
            "share_pct": round(share * 100, 2),
            "lift": round(share / overall_share[top_c], 2),
            "runner_up": cname(second[0]) if second else None,
            "runner_up_share_pct": (
                round(second[1] / n_area * 100, 2) if second else 0
            ),
            "dominated": share >= DOMINANCE_MIN_SHARE,
        })

    dominated = sorted(
        (d for d in dominance_all if d["dominated"]),
        key=lambda d: (-d["share_pct"], sort_key(d["area"])),
    )

    dominating_counts = Counter(d["dominant_cuisine"] for d in dominated)
    cuisines_dominating = [
        {"cuisine": c, "areas": n}
        for c, n in sorted(
            dominating_counts.items(), key=lambda x: (-x[1], x[0].lower())
        )
    ]

    dominance_file = {
        "thresholds": {
            "min_area_restaurants": MIN_AREA_RESTAURANTS,
            "dominance_min_share_pct": round(DOMINANCE_MIN_SHARE * 100, 2),
        },
        "note": (
            "Dominance = share of the area's restaurants that have the "
            "cuisine (absolute share, not compared to the average)."
        ),
        "eligible_areas": len(eligible),
        "dominated_areas_count": len(dominated),
        "cuisines_dominating_areas": cuisines_dominating,
        "dominated_areas": dominated,
        "all_eligible_areas": dominance_all,
    }

    # ====================================================
    # #8: restaurants with more than one cuisine
    # ====================================================
    cuisines_per_restaurant = Counter(
        len(r["cuisines"]) for r in restaurants.values()
    )

    multi_cuisine = sorted(
        (
            {
                "restaurant": d,
                "rep_id": r["rep_id"],
                "num_cuisines": len(r["cuisines"]),
                "cuisines": sorted(cname(c) for c in r["cuisines"]),
            }
            for d, r in restaurants.items()
            if len(r["cuisines"]) >= 2
        ),
        key=lambda x: (-x["num_cuisines"], x["restaurant"].lower()),
    )

    multi_count = len(multi_cuisine)
    multi_pct = safe_div(multi_count * 100, total_restaurants)
    avg_cuisines = safe_div(
        sum(len(r["cuisines"]) for r in restaurants.values()),
        restaurants_with_cuisine,
    )

    multi_cuisine_file = {
        "total_restaurants": total_restaurants,
        "restaurants_with_multiple_cuisines": multi_count,
        "pct_of_restaurants": multi_pct,
        "distribution": {
            str(k): v for k, v in sorted(cuisines_per_restaurant.items())
        },
        "restaurants": multi_cuisine,
    }

    # ====================================================
    # #9: combinations
    # ====================================================
    pair_counter = Counter()
    combo_counter = Counter()

    for r in restaurants.values():
        keys = sorted(r["cuisines"])
        if len(keys) < 2:
            continue
        combo_counter[tuple(keys)] += 1
        for p in combinations(keys, 2):
            pair_counter[p] += 1

    def combo_entries(counter):
        entries = [
            {
                "cuisines": [cname(c) for c in combo],
                "num_cuisines": len(combo),
                "restaurants": n,
                "pct_of_restaurants": safe_div(n * 100, total_restaurants),
            }
            for combo, n in counter.items()
        ]
        entries.sort(key=lambda x: (-x["restaurants"], x["cuisines"]))
        return entries

    all_pairs = combo_entries(pair_counter)
    all_combos = combo_entries(combo_counter)

    combinations_file = {
        "distinct_pairs": len(all_pairs),
        "distinct_full_combinations": len(all_combos),
        "saved_per_list": MAX_COMBINATIONS_SAVED,
        "note": (
            "pairs: every pair of cuisines inside a restaurant. "
            "full_combinations: the whole set of cuisines of a restaurant "
            "counted as one. Only restaurants with 2+ cuisines are used."
        ),
        "top_pairs": all_pairs[:MAX_COMBINATIONS_SAVED],
        "top_full_combinations": all_combos[:MAX_COMBINATIONS_SAVED],
    }

    # ====================================================
    # #10 #11 #12: geographic spread
    # ====================================================
    spread_list = []
    for c, n in cuisine_restaurants.items():
        areas_c = cuisine_areas.get(c, set())

        top_area, top_count = None, 0
        for a in sorted(areas_c, key=sort_key):
            k = area_cuisine[a].get(c, 0)
            if k > top_count:
                top_area, top_count = a, k

        spread_list.append({
            "cuisine": cname(c),
            "restaurants": n,
            "areas": len(areas_c),
            "pct_of_areas": safe_div(len(areas_c) * 100, total_areas),
            "cities": len(cuisine_cities.get(c, ())),
            "top_area": top_area,
            "top_area_city": area_city(top_area) if top_area else None,
            "top_area_share_pct": safe_div(top_count * 100, n),
            "_key": c,
        })

    spread_list.sort(
        key=lambda x: (-x["areas"], -x["restaurants"], x["cuisine"].lower())
    )

    widest = spread_list[:TOP_N_PRINT]

    few_areas = [x for x in spread_list if x["areas"] <= FEW_AREAS_MAX]
    few_areas_single = sum(1 for x in few_areas if x["restaurants"] == 1)
    localized = sorted(
        (x for x in few_areas if x["restaurants"] >= LOCAL_MIN_RESTAURANTS),
        key=lambda x: (-x["restaurants"], x["cuisine"].lower()),
    )

    # #10: الأنواع الموجودة في معظم المناطق (على المناطق الكبيرة بس)
    eligible_pct = {}
    for x in spread_list:
        c = x["_key"]
        present = sum(1 for a in eligible if area_cuisine[a].get(c, 0) > 0)
        eligible_pct[c] = (present, safe_div(present, len(eligible), 4))

    shared = sorted(
        (
            {
                "cuisine": x["cuisine"],
                "restaurants": x["restaurants"],
                "eligible_areas_present": eligible_pct[x["_key"]][0],
                "pct_of_eligible_areas": round(
                    eligible_pct[x["_key"]][1] * 100, 2
                ),
            }
            for x in spread_list
            if eligible
            and eligible_pct[x["_key"]][0]
            >= SHARED_MIN_PCT_OF_AREAS * len(eligible) - 1e-9
        ),
        key=lambda x: (
            -x["pct_of_eligible_areas"], -x["restaurants"], x["cuisine"].lower()
        ),
    )
    in_all_eligible = sum(
        1 for x in shared if x["pct_of_eligible_areas"] >= 100
    )

    for x in spread_list:
        x.pop("_key")

    spread_file = {
        "thresholds": {
            "few_areas_max": FEW_AREAS_MAX,
            "local_min_restaurants": LOCAL_MIN_RESTAURANTS,
            "shared_min_pct_of_areas": round(SHARED_MIN_PCT_OF_AREAS * 100, 2),
            "min_area_restaurants_for_shared": MIN_AREA_RESTAURANTS,
        },
        "total_areas": total_areas,
        "eligible_areas": len(eligible),
        "shared_cuisines": shared,
        "widest_spread_cuisines": widest,
        "cuisines_in_few_areas": few_areas,
        "localized_cuisines": localized,
        "all_cuisines": spread_list,
    }

    # ====================================================
    # threshold sensitivity (كام نتيجة لو غيرتي الحد)
    # ====================================================
    sens_areas = {
        f"areas_with_{t}_or_more_restaurants": sum(
            1 for a in areas_sorted if area_restaurants[a] >= t
        )
        for t in (5, 10, 20, 30, 50)
    }
    sens_few = {
        f"cuisines_in_{t}_areas_or_fewer": sum(
            1 for x in spread_list if x["areas"] <= t
        )
        for t in (1, 2, 3, 5, 10)
    }
    sens_shared = {
        f"cuisines_in_{p}pct_or_more_of_eligible_areas": sum(
            1 for c, (_pr, pc) in eligible_pct.items() if pc * 100 >= p - 1e-9
        )
        for p in (50, 60, 70, 80, 90, 100)
    } if eligible else {}
    sens_dom = {
        f"areas_top_cuisine_{t}pct_or_more": sum(
            1 for d in dominance_all if d["share_pct"] >= t
        )
        for t in (30, 40, 50, 60)
    }
    sens_lift = {
        f"pairs_lift_{x}_or_more": sum(
            1 for cand in candidates if cand["lift"] >= x
        )
        for x in (1.25, 1.5, 2, 3)
    }

    # ====================================================
    # summary
    # ====================================================
    summary = {
        "General info": {
            "files": len(files),
            "total_records": total_records,
        },
        "Restaurants and cuisines (after grouping)": {
            "total_restaurants": total_restaurants,
            "restaurants_with_cuisine": restaurants_with_cuisine,
            "restaurants_without_cuisine": restaurants_without_cuisine,
            "unique_cuisines": len(cuisine_stats),
            "cuisines_with_single_restaurant": cuisines_single_restaurant,
            "restaurants_with_multiple_cuisines": multi_count,
            "restaurants_with_multiple_cuisines_pct": multi_pct,
            "avg_cuisines_per_restaurant": avg_cuisines,
        },
        "Areas": {
            "areas_with_restaurants": total_areas,
            "areas_eligible_for_analysis": len(eligible),
            "min_restaurants_for_eligible": MIN_AREA_RESTAURANTS,
        },
        "Concentration (lift)": {
            "pairs_checked": len(candidates),
            "pairs_qualified": len(qualified),
            "areas_with_concentrated_cuisine": len(conc_by_area),
            "cuisines_concentrated_somewhere": len(conc_by_cuisine),
        },
        "Dominance": {
            "eligible_areas": len(eligible),
            "areas_dominated_by_a_cuisine": len(dominated),
            "dominance_min_share_pct": round(DOMINANCE_MIN_SHARE * 100, 2),
        },
        "Combinations": {
            "distinct_pairs": len(all_pairs),
            "distinct_full_combinations": len(all_combos),
        },
        "Geographic spread": {
            "cuisines_in_few_areas": len(few_areas),
            "cuisines_in_few_areas_with_single_restaurant": few_areas_single,
            "localized_cuisines": len(localized),
            "shared_cuisines": len(shared),
            "cuisines_in_all_eligible_areas": in_all_eligible,
            "few_areas_max": FEW_AREAS_MAX,
            "shared_min_pct_of_areas": round(SHARED_MIN_PCT_OF_AREAS * 100, 2),
        },
        "Check: areas by minimum restaurants": sens_areas,
        "Check: cuisines by number of areas": sens_few,
        "Check: shared cuisines by % of eligible areas": sens_shared,
        "Check: dominance share": sens_dom,
        "Check: concentration lift": sens_lift,
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for section, values in summary.items():
        print(f"\n{section}")
        for k, v in values.items():
            print(f"{k:<46}: {v}")

    # ----------------------------------------------------
    # print-only: معلومات بتتطبع (وبتتحفظ في الـ summary JSON)
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("PRINT-ONLY INFO (also saved in the summary file)")
    print("=" * 70)

    def cuisine_line(x):
        return (
            f"{x['cuisine']}: {x['restaurants']} restaurants "
            f"({x['pct_of_restaurants']}%) | {x['branches']} branches "
            f"({x['pct_of_branches']}%) | {x['areas']} areas"
        )

    print_block(
        "#1 Most widespread cuisines (by number of restaurants)",
        len(cuisine_stats),
        [cuisine_line(x) for x in most_widespread],
        COUNTS_FILE,
    )

    print_block(
        f"#2 Least widespread cuisines "
        f"({cuisines_single_restaurant} cuisines have only 1 restaurant)",
        len(cuisine_stats),
        [cuisine_line(x) for x in least_widespread],
        COUNTS_FILE,
    )

    print("\n#8 Cuisines per restaurant:")
    for n, count in sorted(cuisines_per_restaurant.items()):
        print(f"  {n} cuisine(s): {count} restaurants")
    print(
        f"  -> {multi_count} restaurants ({multi_pct}%) have more than one "
        f"cuisine"
    )

    print_block(
        f"#10 Shared cuisines (in {round(SHARED_MIN_PCT_OF_AREAS * 100)}% or "
        f"more of the {len(eligible)} eligible areas)",
        len(shared),
        [
            f"{x['cuisine']}: {x['eligible_areas_present']} areas "
            f"({x['pct_of_eligible_areas']}%) | {x['restaurants']} restaurants"
            for x in shared[:SHARED_PRINT_MAX]
        ],
        SPREAD_FILE,
    )

    # ----------------------------------------------------
    # SAVE FILES
    # ----------------------------------------------------
    save_json(SUMMARY_FILE, {
        "summary": summary,
        "thresholds": {
            "min_area_restaurants": MIN_AREA_RESTAURANTS,
            "min_cuisine_in_area": MIN_CUISINE_IN_AREA,
            "lift_min": LIFT_MIN,
            "dominance_min_share": DOMINANCE_MIN_SHARE,
            "few_areas_max": FEW_AREAS_MAX,
            "local_min_restaurants": LOCAL_MIN_RESTAURANTS,
            "shared_min_pct_of_areas": SHARED_MIN_PCT_OF_AREAS,
        },
        "most_widespread_cuisines": most_widespread,
        "least_widespread_cuisines": least_widespread,
        "cuisines_per_restaurant_distribution": {
            str(k): v for k, v in sorted(cuisines_per_restaurant.items())
        },
        "shared_cuisines": shared,
    })
    save_json(COUNTS_FILE, counts_file)
    save_json(AREA_MIX_FILE, area_mix_file)
    save_json(CONCENTRATION_FILE, concentration_file)
    save_json(DOMINANCE_FILE, dominance_file)
    save_json(MULTI_CUISINE_FILE, multi_cuisine_file)
    save_json(COMBINATIONS_FILE, combinations_file)
    save_json(SPREAD_FILE, spread_file)

    # ----------------------------------------------------
    # SAMPLES (التفاصيل الكاملة في الملفات)
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print(f"SAMPLES (first {SAMPLES} only, full details are in the files)")
    print("=" * 70)

    # #3
    print_block(
        "#3 Cuisine mix per area (largest areas, top 3 cuisines)",
        len(area_mix),
        [
            f"area {e['area']} (city {e['city']}): {e['restaurants']} "
            f"restaurants, {e['distinct_cuisines']} cuisines | "
            + ", ".join(
                f"{m['cuisine']} {m['pct_of_area_restaurants']}%"
                for m in e["cuisines"][:3]
            )
            for e in area_mix[:SAMPLES]
        ],
        AREA_MIX_FILE,
    )

    # #4 / #5
    print_block(
        "#4 #5 Restaurants and branches per cuisine (first rows)",
        len(cuisine_stats),
        [
            f"{x['cuisine']}: {x['restaurants']} restaurants | "
            f"{x['branches']} branches | avg {x['avg_branches_per_restaurant']}"
            f" branches/restaurant"
            for x in cuisine_stats[:SAMPLES]
        ],
        COUNTS_FILE,
    )

    # #6
    print_block(
        "#6 Concentration by area (top 3 cuisines by lift)",
        len(conc_by_area),
        [
            f"area {e['area']} (city {e['city']}): "
            + ", ".join(
                f"{t['cuisine']} x{t['lift']} ({t['restaurants_in_area']} rest.)"
                for t in e["top_cuisines"][:3]
            )
            for e in conc_by_area[:SAMPLES]
        ],
        CONCENTRATION_FILE,
    )

    print_block(
        "#6 Concentration by cuisine (top 3 areas by lift)",
        len(conc_by_cuisine),
        [
            f"{e['cuisine']}: "
            + ", ".join(
                f"area {t['area']} (city {t['city']}) x{t['lift']}"
                for t in e["top_areas"][:3]
            )
            for e in conc_by_cuisine[:SAMPLES]
        ],
        CONCENTRATION_FILE,
    )

    # #7
    print_block(
        f"#7 Areas dominated by one cuisine "
        f"(>= {round(DOMINANCE_MIN_SHARE * 100)}% of the area's restaurants)",
        len(dominated),
        [
            f"area {d['area']} (city {d['city']}): {d['dominant_cuisine']} "
            f"{d['share_pct']}% (lift {d['lift']}) | runner-up "
            f"{d['runner_up']} {d['runner_up_share_pct']}%"
            for d in dominated[:SAMPLES]
        ],
        DOMINANCE_FILE,
    )

    print_block(
        "#7 Cuisines that dominate the most areas",
        len(cuisines_dominating),
        [f"{x['cuisine']}: {x['areas']} areas" for x in cuisines_dominating[:SAMPLES]],
        DOMINANCE_FILE,
    )

    # #8
    print_block(
        "#8 Restaurants with the most cuisines",
        multi_count,
        [
            f"{m['restaurant']} ({m['num_cuisines']}): "
            f"{short_list(m['cuisines'], 6)}"
            for m in multi_cuisine[:SAMPLES]
        ],
        MULTI_CUISINE_FILE,
    )

    # #9
    print_block(
        "#9 Most common pairs of cuisines",
        len(all_pairs),
        [
            f"{' + '.join(p['cuisines'])}: {p['restaurants']} restaurants "
            f"({p['pct_of_restaurants']}%)"
            for p in all_pairs[:TOP_N_PRINT]
        ],
        COMBINATIONS_FILE,
    )

    print_block(
        "#9 Most common full combinations",
        len(all_combos),
        [
            f"{' + '.join(p['cuisines'])}: {p['restaurants']} restaurants "
            f"({p['pct_of_restaurants']}%)"
            for p in all_combos[:TOP_N_PRINT]
        ],
        COMBINATIONS_FILE,
    )

    # #11
    print_block(
        f"#11 Localized cuisines (>= {LOCAL_MIN_RESTAURANTS} restaurants, "
        f"in {FEW_AREAS_MAX} areas or fewer)",
        len(localized),
        [
            f"{x['cuisine']}: {x['restaurants']} restaurants in "
            f"{x['areas']} area(s) | top area {x['top_area']} "
            f"(city {x['top_area_city']})"
            for x in localized[:TOP_N_PRINT]
        ],
        SPREAD_FILE,
    )

    print_block(
        f"#11 Cuisines in {FEW_AREAS_MAX} areas or fewer "
        f"({few_areas_single} of them have only 1 restaurant)",
        len(few_areas),
        [
            f"{x['cuisine']}: {x['restaurants']} restaurants in "
            f"{x['areas']} area(s)"
            for x in few_areas[:SAMPLES]
        ],
        SPREAD_FILE,
    )

    # #12
    print_block(
        "#12 Cuisines with the widest geographic spread",
        len(spread_list),
        [
            f"{x['cuisine']}: {x['areas']} areas ({x['pct_of_areas']}%) | "
            f"{x['cities']} cities | {x['restaurants']} restaurants | "
            f"top area {x['top_area']} holds {x['top_area_share_pct']}%"
            for x in widest
        ],
        SPREAD_FILE,
    )

    # ----------------------------------------------------
    # SAVED FILES
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{SUMMARY_FILE:<44}: summary + print-only info + thresholds")
    print(f"{COUNTS_FILE:<44}: #1 #2 #4 #5 restaurants/branches per cuisine")
    print(f"{AREA_MIX_FILE:<44}: #3 cuisine mix per area")
    print(f"{CONCENTRATION_FILE:<44}: #6 concentration (lift)")
    print(f"{DOMINANCE_FILE:<44}: #7 dominated areas")
    print(f"{MULTI_CUISINE_FILE:<44}: #8 restaurants with 2+ cuisines")
    print(f"{COMBINATIONS_FILE:<44}: #9 pairs and full combinations")
    print(f"{SPREAD_FILE:<44}: #10 #11 #12 geographic spread")


if __name__ == "__main__":
    main()