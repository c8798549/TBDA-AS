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

SUMMARY_FILE = "market_insights.json"
RESTAURANTS_FILE = "unique_restaurants.json"
BRANCHES_FILE = "unique_branches.json"
GROUPED_FILE = "restaurants_grouped.json"

EXAMPLES_TO_PRINT = 10

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
    """أكتر اسم اتكرر للـ id (لو الـ id له أكتر من اسم)."""
    return counter.most_common(1)[0][0]


def print_examples(title, mapping, label_from, label_to):
    """يطبع عدد + أول N أمثلة."""
    print(f"\n{title} : {len(mapping)}")

    for k, v in list(mapping.items())[:EXAMPLES_TO_PRINT]:
        print(f"  {label_from} = {k}")
        print(f"    {label_to}: {v}")


def split_counts(counts):
    """يقسم المطاعم حسب عدد الفروع."""
    return {
        "multiple_branches": sum(1 for c in counts if c > 1),
        "single_branch": sum(1 for c in counts if c == 1),
        "no_branch_id": sum(1 for c in counts if c == 0),
    }


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

    # restaurant -> its branches
    id_to_branch_ids = defaultdict(set)

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

            # restaurant -> branches
            if _id and b_id:
                id_to_branch_ids[_id].add(b_id)

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

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
    # unique restaurants: [{id, name}, ...]  (واحد لكل id)
    # ----------------------------------------------------
    unique_restaurants = [
        {"id": rid, "name": top_name(id_to_names[rid])}
        for rid in sorted(id_to_names.keys(), key=sort_key)
    ]

    # ----------------------------------------------------
    # unique branches: [{branchId, branchName}, ...]  (واحد لكل branchId)
    # ----------------------------------------------------
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

    for name in names:
        k = brand_key(name)
        if not k:
            continue
        brand_to_names[k].add(name)
        brand_variants[k][first_segment(name)] += 1
        brand_to_ids[k].update(name_to_ids[name])

    grouped = {}
    for k, brand_names in brand_to_names.items():
        display = brand_variants[k].most_common(1)[0][0]

        if display in grouped:
            display = f"{display} ({k})"

        # كل الـ branchIds الفريدة لكل ids المطعم
        brand_branch_ids = set()
        for rid in brand_to_ids[k]:
            brand_branch_ids.update(id_to_branch_ids.get(rid, ()))

        grouped[display] = {
            "unique_names": len(brand_names),
            "unique_ids": len(brand_to_ids[k]),
            "unique_branch_ids": len(brand_branch_ids),
            "name_variants": sorted(brand_variants[k].keys()),
            "ids": sorted(brand_to_ids[k], key=sort_key),
            "names": sorted(brand_names),
        }

    merged = {b: g for b, g in grouped.items() if g["unique_names"] > 1}

    # ----------------------------------------------------
    # restaurants with multiple branches vs single branch
    # ----------------------------------------------------
    by_id_counts = [len(id_to_branch_ids.get(rid, ())) for rid in ids]
    by_group_counts = [g["unique_branch_ids"] for g in grouped.values()]

    by_id_split = split_counts(by_id_counts)
    by_group_split = split_counts(by_group_counts)

    branches_distribution = {
        str(k): v for k, v in sorted(Counter(by_group_counts).items())
    }

    top_by_branches = sorted(
        grouped.items(), key=lambda x: -x[1]["unique_branch_ids"]
    )[:EXAMPLES_TO_PRINT]

    # ----------------------------------------------------
    # summary
    # ----------------------------------------------------
    summary = {
        "files": len(files),
        "total_records": total_records,
        "unique_id": len(ids),
        "unique_name": len(names),
        "unique_name_case_insensitive": len(names_lower),
        "unique_restaurants_after_grouping": len(grouped),
        "restaurants_with_multiple_names": len(merged),
        "unique_branch_id": len(branch_ids),
        "unique_branch_name": len(branch_names),
        "unique_branch_name_case_insensitive": len(branch_names_lower),
        "ids_with_multiple_names": len(multi_name_ids),
        "names_with_multiple_ids": len(multi_id_names),
        "branch_ids_with_multiple_names": len(multi_name_branch_ids),
        "branch_names_with_multiple_ids": len(multi_id_branch_names),
        "by_id_multiple_branches": by_id_split["multiple_branches"],
        "by_id_single_branch": by_id_split["single_branch"],
        "by_id_no_branch_id": by_id_split["no_branch_id"],
        "grouped_multiple_branches": by_group_split["multiple_branches"],
        "grouped_single_branch": by_group_split["single_branch"],
        "grouped_no_branch_id": by_group_split["no_branch_id"],
        "branches_per_restaurant_distribution": branches_distribution,
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        if k == "branches_per_restaurant_distribution":
            continue
        print(f"{k:<38}: {v}")

    # ----------------------------------------------------
    # MULTIPLE BRANCHES vs SINGLE BRANCH
    # ----------------------------------------------------
    print("\n" + "#" * 70)
    print("RESTAURANTS: MULTIPLE BRANCHES vs SINGLE BRANCH")
    print("#" * 70)

    print("\nBy id:")
    print(f"  multiple branches : {by_id_split['multiple_branches']}")
    print(f"  single branch     : {by_id_split['single_branch']}")

    print("\nAfter grouping:")
    print(f"  multiple branches : {by_group_split['multiple_branches']}")
    print(f"  single branch     : {by_group_split['single_branch']}")

    print("\nTop restaurants by number of branches:")
    for brand, info in top_by_branches:
        print(f"  {brand}: {info['unique_branch_ids']} branches")

    # ----------------------------------------------------
    # RESTAURANTS: examples
    # ----------------------------------------------------
    print("\n" + "#" * 70)
    print("RESTAURANTS (id / name)")
    print("#" * 70)

    print_examples(
        "ids with multiple names",
        multi_name_ids, "id", "names"
    )
    print_examples(
        "names with multiple ids",
        multi_id_names, "name", "ids"
    )

    # ----------------------------------------------------
    # BRANCHES: examples
    # ----------------------------------------------------
    print("\n" + "#" * 70)
    print("BRANCHES (branchId / branchName)")
    print("#" * 70)

    print_examples(
        "branchIds with multiple branchNames",
        multi_name_branch_ids, "branchId", "branchNames"
    )
    print_examples(
        "branchNames with multiple branchIds",
        multi_id_branch_names, "branchName", "branchIds"
    )

    # ----------------------------------------------------
    # RESTAURANTS MERGED INTO ONE
    # ----------------------------------------------------
    print("\n" + "#" * 70)
    print(f"RESTAURANTS MERGED INTO ONE ({len(merged)})")
    print("#" * 70)

    for brand, info in sorted(
        merged.items(), key=lambda x: -x[1]["unique_names"]
    ):
        print(
            f"\n{brand}  "
            f"[{info['unique_names']} names | "
            f"{info['unique_ids']} ids | "
            f"{info['unique_branch_ids']} branches]"
        )

        if len(info["name_variants"]) > 1:
            print(f"  spelling variants: {info['name_variants']}")

        for n in info["names"]:
            print(f"    - {n}")

    # ----------------------------------------------------
    # SAVE FILES
    # ----------------------------------------------------
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": summary,
                "restaurants": {
                    "ids_with_multiple_names": multi_name_ids,
                    "names_with_multiple_ids": multi_id_names,
                },
                "branches": {
                    "branch_ids_with_multiple_names": multi_name_branch_ids,
                    "branch_names_with_multiple_ids": multi_id_branch_names,
                },
            },
            f, indent=2, ensure_ascii=False,
        )

    with open(RESTAURANTS_FILE, "w", encoding="utf-8") as f:
        json.dump(unique_restaurants, f, indent=2, ensure_ascii=False)

    with open(BRANCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(unique_branches, f, indent=2, ensure_ascii=False)

    with open(GROUPED_FILE, "w", encoding="utf-8") as f:
        json.dump(grouped, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{SUMMARY_FILE:<28}: summary + multiple-mapping details")
    print(f"{RESTAURANTS_FILE:<28}: {len(unique_restaurants)} {{id, name}}")
    print(f"{BRANCHES_FILE:<28}: {len(unique_branches)} {{branchId, branchName}}")
    print(f"{GROUPED_FILE:<28}: {len(grouped)} restaurants after grouping")


if __name__ == "__main__":
    main()