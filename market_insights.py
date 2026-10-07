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
MERGED_BY_ID_FILE = "restaurants_merged_by_id.json"
BRANCH_COUNTS_FILE = "restaurant_branch_counts.json"
MULTI_BRANCHES_FILE = "restaurants_multiple_branches.json"
CUISINE_FILE = "cuisine_distribution.json"
CITY_DISTRIBUTION_FILE = "restaurant_city_distribution.json"
BRANCH_DISTRIBUTION_BY_AREA_FILE = "branch_distribution_by_area.json"
RESTAURANT_DISTRIBUTION_BY_AREA_FILE = "restaurant_distribution_by_area.json"
RESTAURANT_DENSITY_EXTREMES_FILE = "restaurant_density_extremes.json"

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
    id_to_cities = defaultdict(set)
    area_to_restaurants = defaultdict(set)
    

    # branches (branchId / branchName)
    branch_ids, branch_names, branch_names_lower = set(), set(), set()
    branch_id_to_names = defaultdict(Counter)
    branch_name_to_ids = defaultdict(set)
    # shopArea -> cities
    area_to_cities = defaultdict(set)
    area_to_branch_ids = defaultdict(set)

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
            shop_city = clean(r.get("shopCity"))
            shop_area = clean(r.get("shopArea"))

            # restaurants
            if _id:
                ids.add(_id)
            if name:
                names.add(name)
                names_lower.add(name.lower())
            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)

            # restaurant cities
            if _id and shop_city:
                id_to_cities[_id].add(shop_city)

            # shopArea -> restaurants
            if _id and shop_area:
                area_to_restaurants[shop_area].add(_id)

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

            #shopArea
            if shop_area and b_id:
                area_to_branch_ids[shop_area].add(b_id)

            # shopArea -> city
            if shop_area and shop_city:
                area_to_cities[shop_area].add(shop_city)
            

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
    # group restaurants
    #   القاعدة 1: الجزء الأول من الاسم (قبل أول فاصلة)
    #   القاعدة 2: أي أسماء ظهرت مع نفس الـ id تتدمج (id = المطعم)
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

    # union-find: ندمج المفاتيح اللي ظهرت تحت نفس الـ id
    parent = {k: k for k in brand_to_names}

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
    for k in brand_to_names:
        components[find(k)].append(k)

    grouped = {}            # restaurants after grouping
    branches_grouped = {}   # branches per restaurant after grouping
    rep_ids = {}            # restaurant (display) -> representative id
    brand_cuisines = {}     # restaurant (display) -> set of cuisines
    branch_to_brands = defaultdict(set)   # branchId -> المطاعم اللي ظهر فيها
    used_rep_ids = set()

    # الفروع الفريدة بعد التجميع (set بتشيل التكرار)
    grouped_branch_ids = set()
    grouped_branch_names = set()
    grouped_branch_names_lower = set()

    for root in sorted(components):
        keys = sorted(components[root])

        brand_names = set()
        comp_ids = set()
        variants = Counter()
        for k in keys:
            brand_names |= brand_to_names[k]
            comp_ids |= brand_to_ids[k]
            variants.update(brand_variants[k])

        # اسم العرض: الأكتر تكرارًا، ولو تعادل الأقصر
        display = sorted(
            variants.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

        if display in grouped:
            display = f"{display} ({root})"

        # id تمثيلي للمطعم = أقل id من ids المطعم
        candidates = sorted(comp_ids, key=sort_key)
        rep_id = next(
            (c for c in candidates if c not in used_rep_ids), None
        )
        if rep_id is None:
            rep_id = candidates[0] if candidates else f"no_id:{root}"
        used_rep_ids.add(rep_id)
        rep_ids[display] = rep_id

        # فروع المطعم (بعد التجميع): branchId -> Counter(branchName)
        brand_branches = {}
        cuisines = set()
        for rid in comp_ids:
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

        grouped_branch_ids.update(brand_branches)
        grouped_branch_names.update(b_names)
        grouped_branch_names_lower.update(b_names_lower)

        grouped[display] = {
            "unique_names": len(brand_names),
            "unique_ids": len(comp_ids),
            "unique_branch_ids": len(brand_branches),
            "merged_by_shared_id": len(keys) > 1,
            "name_variants": sorted(variants.keys()),
            "ids": sorted(comp_ids, key=sort_key),
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

    # المطاعم اللي اتدمجت بسبب إن نفس الـ id ظهر بأسماء مختلفة
    merged_by_id = {
        b: {
            "unique_ids": g["unique_ids"],
            "unique_branch_ids": g["unique_branch_ids"],
            "name_variants": g["name_variants"],
            "ids": g["ids"],
            "names": g["names"],
        }
        for b, g in grouped.items()
        if g["merged_by_shared_id"]
    }

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
    total_branches = len(grouped_branch_ids)

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

    # ---------------------------------------------------------
    # Restaurant distribution by city (after grouping)
    # ---------------------------------------------------------

    city_to_restaurants = defaultdict(set)

    for display, group in grouped.items():
        group_ids = group["ids"]

        group_cities = set()

        for rid in group_ids:
            group_cities.update(id_to_cities.get(rid, set()))

        for city in group_cities:
            city_to_restaurants[city].add(display)

    restaurant_city_distribution = []

    total_grouped_restaurants = len(grouped)

    for city in sorted(city_to_restaurants, key=sort_key):
        restaurant_count = len(city_to_restaurants[city])

        percentage = safe_div(
            restaurant_count * 100,
            total_grouped_restaurants
        )

        restaurant_city_distribution.append({
            "shopCity": city,
            "restaurant_count": restaurant_count,
            "percentage": percentage
        })

    # ----------------------------------------------------
    # Branch distribution by area
    # ----------------------------------------------------

    branch_distribution_by_area = defaultdict(list)

    for area in sorted(area_to_branch_ids, key=sort_key):
        cities = area_to_cities.get(area, set())

        if not cities:
            continue

        city = sorted(cities, key=sort_key)[0]

        branch_distribution_by_area[city].append({
            "shopArea": area,
            "branch_count": len(area_to_branch_ids[area])
        })

    branch_distribution_by_area = {
        city: {
            "areas": areas
        }
        for city, areas in sorted(
            branch_distribution_by_area.items(),
            key=lambda x: sort_key(x[0])
        )
    }

    # ----------------------------------------------------
    # Restaurant distribution by area
    # ----------------------------------------------------

    area_to_grouped_restaurants = defaultdict(set)

    restaurant_to_areas = defaultdict(set)

    for area, restaurant_ids in area_to_restaurants.items():
        for rid in restaurant_ids:
            restaurant_to_areas[rid].add(area)

    for display, group in grouped.items():
        group_ids = group["ids"]
        group_areas = set()

        for rid in group_ids:
            group_areas.update(
                restaurant_to_areas.get(rid, set())
            )

        for area in group_areas:
            area_to_grouped_restaurants[area].add(display)


    restaurant_distribution_by_area = defaultdict(list)

    total_grouped_restaurants = len(grouped)

    for area in sorted(area_to_grouped_restaurants, key=sort_key):
        cities = area_to_cities.get(area, set())

        if not cities:
            continue

        city = sorted(cities, key=sort_key)[0]

        restaurant_count = len(
            area_to_grouped_restaurants[area]
        )

        percentage = safe_div(
            restaurant_count * 100,
            total_grouped_restaurants
        )

        restaurant_distribution_by_area[city].append({
            "shopArea": area,
            "restaurant_count": restaurant_count,
            "percentage": percentage
        })

    restaurant_distribution_by_area = {
        city: {
            "areas": areas
        }
        for city, areas in sorted(
            restaurant_distribution_by_area.items(),
            key=lambda x: sort_key(x[0])
        )
    }

    # ----------------------------------------------------
    # highest and lowest density
    # ----------------------------------------------------
    all_area_restaurant_distribution = []

    for city, city_data in restaurant_distribution_by_area.items():
        for area in city_data["areas"]:
            all_area_restaurant_distribution.append({
                "shopCity": city,
                "shopArea": area["shopArea"],
                "restaurant_count": area["restaurant_count"],
                "percentage": area["percentage"]
            })

    highest_density_areas = sorted(
        all_area_restaurant_distribution,
        key=lambda x: (
            -x["restaurant_count"],
            sort_key(x["shopCity"]),
            sort_key(x["shopArea"])
        )
    )[:10]

    lowest_density_areas = sorted(
        all_area_restaurant_distribution,
        key=lambda x: (
            x["restaurant_count"],
            sort_key(x["shopCity"]),
            sort_key(x["shopArea"])
        )
    )[:10]

    restaurant_density_extremes = {
        "highest_density_areas": highest_density_areas,
        "lowest_density_areas": lowest_density_areas
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
            "restaurants_merged_by_shared_id": len(merged_by_id),
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
            "unique_branch_id": len(grouped_branch_ids),
            "unique_branch_name": len(grouped_branch_names),
            "unique_branch_name_case_insensitive": len(
                grouped_branch_names_lower
            ),
            "branch_ids_in_multiple_restaurants": len(shared_branch_ids),
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

        "Restaurant Distribution by City":{
            "restaurant_distribution_by_city": restaurant_city_distribution,
        },

        "Branch Distribution by Area": {
            "branch_distribution_by_area": branch_distribution_by_area,
        },

        "Restaurant Distribution by Area": {
            "restaurant_distribution_by_area": restaurant_distribution_by_area,
        },

        "Restaurant Density Extremes": {
            "highest_density_areas": highest_density_areas,
            "lowest_density_areas": lowest_density_areas,
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

            if section == "Restaurant Distribution by City" and k == "restaurant_distribution_by_city":
                for city in v:
                    print(
                        f"  City {city['shopCity']:<3}: "
                        f"{city['restaurant_count']:,} restaurants "
                        f"({city['percentage']:.2f}%)"
                    )

            elif section == "Branch Distribution by Area" and k == "branch_distribution_by_area":
                for city, city_data in v.items():
                    print(f"  City {city}")

                    for area in city_data["areas"]:
                        print(
                            f"    Area {area['shopArea']:<6}: "
                            f"{area['branch_count']:,} branches"
                        )

            elif section == "Restaurant Density Extremes":
                print("\n  Top 10 highest-density areas:")

                for area in values["highest_density_areas"]:
                    print(
                        f"    City {area['shopCity']}, "
                        f"Area {area['shopArea']}: "
                        f"{area['restaurant_count']:,} restaurants "
                        f"({area['percentage']:.2f}%)"
                    )

                print("\n  Bottom 10 lowest-density areas:")

                for area in values["lowest_density_areas"]:
                    print(
                        f"    City {area['shopCity']}, "
                        f"Area {area['shopArea']}: "
                        f"{area['restaurant_count']:,} restaurants "
                        f"({area['percentage']:.2f}%)"
                    )

            else:
                print(f"{k:<38}: {v}")

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
    save_json(MERGED_BY_ID_FILE, merged_by_id)
    save_json(BRANCH_COUNTS_FILE, restaurant_branch_counts)
    save_json(MULTI_BRANCHES_FILE, restaurants_multiple_branches)
    save_json(CUISINE_FILE, cuisine_distribution)
    save_json(CITY_DISTRIBUTION_FILE,restaurant_city_distribution)
    save_json(BRANCH_DISTRIBUTION_BY_AREA_FILE,branch_distribution_by_area)
    save_json(RESTAURANT_DISTRIBUTION_BY_AREA_FILE,restaurant_distribution_by_area)
    save_json(RESTAURANT_DENSITY_EXTREMES_FILE,restaurant_density_extremes)

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
        "Restaurant distribution by city",
        len(restaurant_city_distribution),
        [
            f"{c['shopCity']}: {c['restaurant_count']} restaurants "
            f"({c['percentage']}%)"
            for c in restaurant_city_distribution
        ],
        CITY_DISTRIBUTION_FILE,
    )

    print("\nBranch distribution by area")

    total_areas = sum(
        len(city_data["areas"])
        for city_data in branch_distribution_by_area.values()
    )

    print(f"  Total areas: {total_areas}")
    print(f"  Total cities: {len(branch_distribution_by_area)}")

    for city, city_data in branch_distribution_by_area.items():
        print(f"\n  City {city}")

        for area in city_data["areas"]:
            print(
                f"    Area {area['shopArea']:<6}: "
                f"{area['branch_count']:,} branches"
            )

    print(
        f"\n  -> full details: "
        f"{BRANCH_DISTRIBUTION_BY_AREA_FILE}"
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

    # restaurants merged because the same id has different names
    top_by_id = sorted(
        merged_by_id.items(),
        key=lambda x: -len(x[1]["name_variants"]),
    )[:SAMPLES]
    print_block(
        "Restaurants merged because the same id has different names",
        len(merged_by_id),
        [
            f"{b} <- {short_list(info['name_variants'])}"
            for b, info in top_by_id
        ],
        MERGED_BY_ID_FILE,
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
    print(f"{CITY_DISTRIBUTION_FILE:<32}: restaurant distribution by city")
    print(f"{BRANCH_DISTRIBUTION_BY_AREA_FILE:<32}: branch distribution by area")
    print(f"{RESTAURANT_DISTRIBUTION_BY_AREA_FILE:<32}: restaurant distribution by area")
    print(f"{RESTAURANT_DENSITY_EXTREMES_FILE:<32}: Top 10 and Bottom 10 areas by restaurant count")
    print(f"{CUISINE_FILE:<32}: restaurants per cuisine")
    print(f"{DUPLICATES_FILE:<32}: the 5 multiple-mapping lists")
    print(f"{MULTI_NAMES_FILE:<32}: {len(merged)} restaurants with multiple names")
    print(f"{MERGED_BY_ID_FILE:<32}: {len(merged_by_id)} restaurants merged by shared id")


if __name__ == "__main__":
    main()