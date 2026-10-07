import os
import re
import json
import unicodedata
from collections import defaultdict, Counter

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

# ---------------- output files ----------------
SUMMARY_FILE = "market_insights.json"
RESTAURANTS_FILE = "unique_restaurants.json"
RESTAURANTS_GROUPED_FILE = "restaurants_grouped.json"
BRANCHES_FILE = "unique_branches.json"
BRANCHES_GROUPED_FILE = "branches_grouped.json"
DUPLICATES_FILE = "duplicates_details.json"
MULTI_NAMES_FILE = "restaurants_multiple_names.json"
BRANCH_COUNTS_FILE = "restaurant_branch_counts.json"
MULTI_BRANCHES_FILE = "restaurants_multiple_branches.json"
CUISINE_FILE = "cuisine_distribution.json"

SAMPLES = 5            # عدد الأمثلة اللي بتتطبع في اللوج
CUISINE_SAMPLES = 10   # عدد الـ cuisines اللي بتتطبع في اللوج

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
    """أكتر اسم اتكرر (None لو مفيش أسماء)."""
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def split_counts(counts):
    """يقسم المطاعم حسب عدد الفروع."""
    return {
        "multiple_branches": sum(1 for c in counts if c > 1),
        "single_branch": sum(1 for c in counts if c == 1),
        "no_branch_id": sum(1 for c in counts if c == 0),
    }


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


def print_block(title, total, lines, filename):
    """عنوان + عدد + samples + مكان الملف الكامل."""
    print(f"\n{title} ({total})")

    for line in lines:
        print(f"  {line}")

    if total > len(lines):
        print(f"  ... and {total - len(lines)} more")

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
    # restaurants (id / name)
    ids, names, names_lower = set(), set(), set()
    id_to_names = defaultdict(Counter)
    name_to_ids = defaultdict(set)

    # branches (branchId / branchName)
    branch_ids, branch_names, branch_names_lower = set(), set(), set()
    branch_id_to_names = defaultdict(Counter)
    branch_name_to_ids = defaultdict(set)

    # (restaurant id, branchId) -> Counter(branchName)
    pair_names = defaultdict(Counter)

    # cuisines: id -> set of cuisines (lowercase key) + شكل الكتابة
    id_to_cuisines = defaultdict(set)
    cuisine_forms = defaultdict(Counter)

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
            b_name = clean(r.get("branchName"))
            cuisine_str = clean(r.get("cuisineString"))

            # restaurants
            if _id:
                ids.add(_id)
            if name:
                names.add(name)
                names_lower.add(name.lower())
            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)

            # branches
            if b_id:
                branch_ids.add(b_id)
            if b_name:
                branch_names.add(b_name)
                branch_names_lower.add(b_name.lower())
            if b_id and b_name:
                branch_id_to_names[b_id][b_name] += 1
                branch_name_to_ids[b_name].add(b_id)

            # restaurant -> branch
            if _id and b_id:
                counter = pair_names[(_id, b_id)]
                if b_name:
                    counter[b_name] += 1

            # cuisines (نقسم على الفاصلة)
            if _id and cuisine_str:
                for part in cuisine_str.split(","):
                    p = re.sub(r"\s+", " ", part).strip()
                    if not p:
                        continue
                    ck = p.lower()
                    id_to_cuisines[_id].add(ck)
                    cuisine_forms[ck][p] += 1

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    # restaurant id -> its branchIds
    id_to_branch_ids = defaultdict(set)
    for (rid, bid) in pair_names:
        id_to_branch_ids[rid].add(bid)

    # ----------------------------------------------------
    # multiple mappings
    # ----------------------------------------------------
    multi_name_ids = {
        k: sorted(v.keys())
        for k, v in sorted(id_to_names.items(), key=lambda x: sort_key(x[0]))
        if len(v) > 1
    }
    multi_id_names = {
        k: sorted(v, key=sort_key)
        for k, v in sorted(name_to_ids.items())
        if len(v) > 1
    }
    multi_name_branch_ids = {
        k: sorted(v.keys())
        for k, v in sorted(
            branch_id_to_names.items(), key=lambda x: sort_key(x[0])
        )
        if len(v) > 1
    }
    multi_id_branch_names = {
        k: sorted(v, key=sort_key)
        for k, v in sorted(branch_name_to_ids.items())
        if len(v) > 1
    }

    # ----------------------------------------------------
    # unique restaurants / branches  (واحد لكل id)
    # ----------------------------------------------------
    unique_restaurants = [
        {"id": rid, "name": top_name(id_to_names[rid])}
        for rid in sorted(id_to_names.keys(), key=sort_key)
    ]

    unique_branches = [
        {"branchId": bid, "branchName": top_name(branch_id_to_names[bid])}
        for bid in sorted(branch_id_to_names.keys(), key=sort_key)
    ]

    # ----------------------------------------------------
    # group restaurants (الجزء الأول من الاسم)
    # ----------------------------------------------------
    brand_to_names = defaultdict(set)
    brand_to_ids = defaultdict(set)
    brand_variants = defaultdict(Counter)

    for name in sorted(names):      # sorted عشان النتيجة تبقى ثابتة
        k = brand_key(name)
        if not k:
            continue
        brand_to_names[k].add(name)
        brand_variants[k][first_segment(name)] += 1
        brand_to_ids[k].update(name_to_ids[name])

    grouped = {}            # restaurants after grouping
    branches_grouped = {}   # branches after grouping
    rep_ids = {}            # restaurant (display) -> representative id
    brand_cuisines = {}     # restaurant (display) -> set of cuisines
    branch_to_brands = defaultdict(set)   # branchId -> المطاعم اللي ظهر فيها
    used_rep_ids = set()

    for k in sorted(brand_to_names):
        brand_names = brand_to_names[k]
        display = brand_variants[k].most_common(1)[0][0]

        if display in grouped:
            display = f"{display} ({k})"

        # id تمثيلي للمطعم = أقل id من ids المطعم
        candidates = sorted(brand_to_ids[k], key=sort_key)
        rep_id = next(
            (c for c in candidates if c not in used_rep_ids), None
        )
        if rep_id is None:
            rep_id = candidates[0] if candidates else f"no_id:{k}"
        used_rep_ids.add(rep_id)
        rep_ids[display] = rep_id

        # فروع المطعم (بعد التجميع): branchId -> Counter(branchName)
        brand_branches = {}
        cuisines = set()
        for rid in brand_to_ids[k]:
            for bid in id_to_branch_ids.get(rid, ()):
                brand_branches.setdefault(bid, Counter()).update(
                    pair_names[(rid, bid)]
                )
            cuisines.update(id_to_cuisines.get(rid, ()))

        brand_cuisines[display] = cuisines

        # نسجل كل فرع ظهر تحت أنهي مطعم
        for bid in brand_branches:
            branch_to_brands[bid].add(display)

        b_names = set()
        for c in brand_branches.values():
            b_names.update(c.keys())
        b_names_lower = {n.lower() for n in b_names}

        grouped[display] = {
            "unique_names": len(brand_names),
            "unique_ids": len(brand_to_ids[k]),
            "unique_branch_ids": len(brand_branches),
            "name_variants": sorted(brand_variants[k].keys()),
            "ids": sorted(brand_to_ids[k], key=sort_key),
            "names": sorted(brand_names),
        }

        branches_grouped[display] = {
            "unique_branch_ids": len(brand_branches),
            "unique_branch_names": len(b_names),
            "unique_branch_names_case_insensitive": len(b_names_lower),
            "branches": [
                {
                    "branchId": bid,
                    "branchName": top_name(brand_branches[bid]),
                }
                for bid in sorted(brand_branches, key=sort_key)
            ],
        }

    # المطاعم اللي اتجمع فيها أكتر من اسم
    merged = {b: g for b, g in grouped.items() if g["unique_names"] > 1}

    # ----------------------------------------------------
    # branch ids shared between more than one restaurant
    # ----------------------------------------------------
    shared_branch_ids = {
        bid: sorted(brands)
        for bid, brands in sorted(
            branch_to_brands.items(), key=lambda x: sort_key(x[0])
        )
        if len(brands) > 1
    }
    extra_branches_counted = sum(
        len(b) - 1 for b in shared_branch_ids.values()
    )

    # ----------------------------------------------------
    # branches per restaurant (after grouping)
    # ----------------------------------------------------
    restaurant_branch_counts = {}        # كل المطاعم
    restaurants_multiple_branches = {}   # اللي ليها أكتر من فرع

    ordered = sorted(
        grouped.items(),
        key=lambda x: (-x[1]["unique_branch_ids"], x[0]),
    )

    for display, g in ordered:
        rid = rep_ids[display]

        restaurant_branch_counts[rid] = {
            "restaurant": display,
            "num_branches": g["unique_branch_ids"],
            "num_ids": g["unique_ids"],
        }

        if g["unique_branch_ids"] > 1:
            restaurants_multiple_branches[rid] = {
                "restaurant": display,
                "num_branches": g["unique_branch_ids"],
                "ids": g["ids"],
                "branches": branches_grouped[display]["branches"],
            }

    # ----------------------------------------------------
    # multiple branches vs single branch
    # ----------------------------------------------------
    by_id_counts = [len(id_to_branch_ids.get(rid, ())) for rid in ids]
    by_group_counts = [g["unique_branch_ids"] for g in grouped.values()]

    by_id_split = split_counts(by_id_counts)
    by_group_split = split_counts(by_group_counts)

    branches_distribution = {
        str(k): v for k, v in sorted(Counter(by_group_counts).items())
    }

    # ----------------------------------------------------
    # average / ratio (after grouping)
    # ----------------------------------------------------
    total_restaurants = len(grouped)
    total_branches = sum(by_group_counts)

    ag_branch_ids = sum(
        v["unique_branch_ids"] for v in branches_grouped.values()
    )
    ag_branch_names = sum(
        v["unique_branch_names"] for v in branches_grouped.values()
    )
    ag_branch_names_ci = sum(
        v["unique_branch_names_case_insensitive"]
        for v in branches_grouped.values()
    )

    avg_branches = safe_div(total_branches, total_restaurants)
    restaurants_pct_of_branches = safe_div(
        total_restaurants * 100, total_branches
    )
    multi_pct = safe_div(
        by_group_split["multiple_branches"] * 100, total_restaurants
    )
    single_pct = safe_div(
        by_group_split["single_branch"] * 100, total_restaurants
    )

    # ----------------------------------------------------
    # cuisine distribution (after grouping)
    # ----------------------------------------------------
    cuisine_counter = Counter()
    for cuisines in brand_cuisines.values():
        for c in cuisines:
            cuisine_counter[c] += 1

    cuisines_per_restaurant = Counter(
        len(c) for c in brand_cuisines.values()
    )

    with_cuisine = sum(1 for c in brand_cuisines.values() if c)
    without_cuisine = total_restaurants - with_cuisine

    restaurants_per_cuisine = [
        {
            "cuisine": top_name(cuisine_forms[c]),
            "restaurants": n,
            "percentage_of_restaurants": safe_div(
                n * 100, total_restaurants
            ),
        }
        for c, n in sorted(
            cuisine_counter.items(), key=lambda x: (-x[1], x[0])
        )
    ]

    cuisine_distribution = {
        "total_restaurants": total_restaurants,
        "restaurants_with_cuisine": with_cuisine,
        "restaurants_without_cuisine": without_cuisine,
        "unique_cuisines": len(cuisine_counter),
        "note": (
            "A restaurant can have several cuisines, so it is counted under "
            "each one. The percentages can add up to more than 100."
        ),
        "restaurants_per_cuisine": restaurants_per_cuisine,
        "cuisines_per_restaurant_distribution": {
            str(k): v for k, v in sorted(cuisines_per_restaurant.items())
        },
    }

    # ----------------------------------------------------
    # summary
    # ----------------------------------------------------
    summary = {
        "General info": {
            "files": len(files),
            "total_records": total_records,
        },
        "Number of restaurants": {
            "unique_id": len(ids),
            "unique_name": len(names),
            "unique_name_case_insensitive": len(names_lower),
        },
        "Number of restaurants after grouping": {
            "unique_restaurants_after_grouping": len(grouped),
            "restaurants_with_multiple_names": len(merged),
        },
        "Number of branches": {
            "unique_branch_id": len(branch_ids),
            "unique_branch_name": len(branch_names),
            "unique_branch_name_case_insensitive": len(branch_names_lower),
            "by_id_multiple_branches": by_id_split["multiple_branches"],
            "by_id_single_branch": by_id_split["single_branch"],
            "by_id_no_branch_id": by_id_split["no_branch_id"],
        },
        "Number of branches after grouping": {
            "unique_branch_id": ag_branch_ids,
            "unique_branch_name": ag_branch_names,
            "unique_branch_name_case_insensitive": ag_branch_names_ci,
            "branch_ids_in_multiple_restaurants": len(shared_branch_ids),
            "extra_branches_counted_after_grouping": extra_branches_counted,
            "grouped_multiple_branches": by_group_split["multiple_branches"],
            "grouped_single_branch": by_group_split["single_branch"],
            "grouped_no_branch_id": by_group_split["no_branch_id"],
        },
        "Branches per restaurant (after grouping)": {
            "total_restaurants": total_restaurants,
            "total_branches": total_branches,
            "avg_branches_per_restaurant": avg_branches,
            "ratio_restaurants_to_branches": f"1 : {avg_branches}",
            "restaurants_pct_of_branches": restaurants_pct_of_branches,
            "restaurants_multiple_branches_pct": multi_pct,
            "restaurants_single_branch_pct": single_pct,
        },
        "Cuisines (after grouping)": {
            "unique_cuisines": len(cuisine_counter),
            "restaurants_with_cuisine": with_cuisine,
            "restaurants_without_cuisine": without_cuisine,
        },
        "Some details about duplicates": {
            "ids_with_multiple_names": len(multi_name_ids),
            "names_with_multiple_ids": len(multi_id_names),
            "branch_ids_with_multiple_names": len(multi_name_branch_ids),
            "branch_names_with_multiple_ids": len(multi_id_branch_names),
        },
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for section, values in summary.items():
        print(f"\n{section}")
        for k, v in values.items():
            print(f"{k:<38}: {v}")

    print("\n")

    # ----------------------------------------------------
    # SAVE FILES
    # ----------------------------------------------------
    save_json(SUMMARY_FILE, {
        "summary": summary,
        "branches_per_restaurant_distribution": branches_distribution,
    })
    save_json(RESTAURANTS_FILE, unique_restaurants)
    save_json(RESTAURANTS_GROUPED_FILE, grouped)
    save_json(BRANCHES_FILE, unique_branches)
    save_json(BRANCHES_GROUPED_FILE, branches_grouped)
    save_json(DUPLICATES_FILE, {
        "ids_with_multiple_names": multi_name_ids,
        "names_with_multiple_ids": multi_id_names,
        "branch_ids_with_multiple_names": multi_name_branch_ids,
        "branch_names_with_multiple_ids": multi_id_branch_names,
        "branch_ids_in_multiple_restaurants": shared_branch_ids,
    })
    save_json(MULTI_NAMES_FILE, merged)
    save_json(BRANCH_COUNTS_FILE, restaurant_branch_counts)
    save_json(MULTI_BRANCHES_FILE, restaurants_multiple_branches)
    save_json(CUISINE_FILE, cuisine_distribution)

    # ----------------------------------------------------
    # SAMPLES (التفاصيل الكاملة في الملفات)
    # ----------------------------------------------------
    print("=" * 70)
    print(f"SAMPLES (first {SAMPLES} only, full details are in the files)")
    print("=" * 70)

    print_block(
        "Unique restaurants (id | name)",
        len(unique_restaurants),
        [f"{r['id']} | {r['name']}" for r in unique_restaurants[:SAMPLES]],
        RESTAURANTS_FILE,
    )

    top_grouped = sorted(
        grouped.items(), key=lambda x: -x[1]["unique_branch_ids"]
    )[:SAMPLES]
    print_block(
        "Unique restaurants after grouping (top by branches)",
        len(grouped),
        [
            f"{b} [{g['unique_ids']} ids | "
            f"{g['unique_branch_ids']} branches]"
            for b, g in top_grouped
        ],
        RESTAURANTS_GROUPED_FILE,
    )

    print_block(
        "Unique branches (branchId | branchName)",
        len(unique_branches),
        [
            f"{b['branchId']} | {b['branchName']}"
            for b in unique_branches[:SAMPLES]
        ],
        BRANCHES_FILE,
    )

    top_branches_grouped = sorted(
        branches_grouped.items(), key=lambda x: -x[1]["unique_branch_ids"]
    )[:SAMPLES]
    print_block(
        "Unique branches after grouping (top restaurants)",
        len(branches_grouped),
        [
            f"{b}: {g['unique_branch_ids']} branch ids"
            for b, g in top_branches_grouped
        ],
        BRANCHES_GROUPED_FILE,
    )

    print_block(
        "Branches per restaurant (restaurant_id | restaurant: branches)",
        len(restaurant_branch_counts),
        [
            f"{rid} | {v['restaurant']}: {v['num_branches']} branches"
            for rid, v in list(restaurant_branch_counts.items())[:SAMPLES]
        ],
        BRANCH_COUNTS_FILE,
    )

    print_block(
        "Restaurants with more than one branch",
        len(restaurants_multiple_branches),
        [
            f"{rid} | {v['restaurant']}: {v['num_branches']} branches"
            for rid, v in list(restaurants_multiple_branches.items())[:SAMPLES]
        ],
        MULTI_BRANCHES_FILE,
    )

    print_block(
        "Restaurants per cuisine (top)",
        len(restaurants_per_cuisine),
        [
            f"{c['cuisine']}: {c['restaurants']} restaurants "
            f"({c['percentage_of_restaurants']}%)"
            for c in restaurants_per_cuisine[:CUISINE_SAMPLES]
        ],
        CUISINE_FILE,
    )

    print("\nCuisines per restaurant:")
    for n, count in sorted(cuisines_per_restaurant.items()):
        print(f"  {n} cuisine(s): {count} restaurants")

    print_block(
        "ids with multiple names",
        len(multi_name_ids),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_name_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "names with multiple ids",
        len(multi_id_names),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_id_names.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch ids with multiple names",
        len(multi_name_branch_ids),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_name_branch_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch names with multiple ids",
        len(multi_id_branch_names),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_id_branch_names.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch ids shared by more than one restaurant",
        len(shared_branch_ids),
        [
            f"{bid} -> {short_list(brands)}"
            for bid, brands in list(shared_branch_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )

    # restaurants merged into one (top by number of names)
    top_merged = sorted(
        merged.items(), key=lambda x: -x[1]["unique_names"]
    )[:SAMPLES]

    print(f"\nRESTAURANTS MERGED INTO ONE ({len(merged)})")
    for brand, info in top_merged:
        print(
            f"\n  {brand}  "
            f"[{info['unique_names']} names | "
            f"{info['unique_ids']} ids | "
            f"{info['unique_branch_ids']} branches]"
        )
        if len(info["name_variants"]) > 1:
            print(f"    spelling variants: {info['name_variants']}")
        for n in info["names"][:3]:
            print(f"      - {n}")
        if len(info["names"]) > 3:
            print(f"      ... +{len(info['names']) - 3} more")

    if len(merged) > SAMPLES:
        print(f"\n  ... and {len(merged) - SAMPLES} more")
    print(f"  -> full details: {MULTI_NAMES_FILE}")

    # ----------------------------------------------------
    # SAVED FILES
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{SUMMARY_FILE:<32}: summary numbers")
    print(f"{RESTAURANTS_FILE:<32}: {len(unique_restaurants)} {{id, name}}")
    print(f"{RESTAURANTS_GROUPED_FILE:<32}: {len(grouped)} restaurants after grouping")
    print(f"{BRANCHES_FILE:<32}: {len(unique_branches)} {{branchId, branchName}}")
    print(f"{BRANCHES_GROUPED_FILE:<32}: branches per grouped restaurant")
    print(f"{BRANCH_COUNTS_FILE:<32}: {len(restaurant_branch_counts)} restaurants -> num of branches")
    print(f"{MULTI_BRANCHES_FILE:<32}: {len(restaurants_multiple_branches)} restaurants with more than one branch")
    print(f"{CUISINE_FILE:<32}: restaurants per cuisine")
    print(f"{DUPLICATES_FILE:<32}: the 5 multiple-mapping lists")
    print(f"{MULTI_NAMES_FILE:<32}: {len(merged)} restaurants with multiple names")


if __name__ == "__main__":
    main()