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
OUTPUT_FILE = "unique_counts.json"

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]

APOSTROPHES = re.compile(r"[\'`´‘’ʼ′ʻ]")


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


def main():
    # list files
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                files.append(obj["Key"])
    files.sort()
    print(f"Files found: {len(files)}")

    ids, names, names_lower = set(), set(), set()
    id_to_names = defaultdict(set)
    name_to_ids = defaultdict(set)
    total_records = 0

    for i, key in enumerate(files, start=1):
        records = load_records(key)
        total_records += len(records)

        for r in records:
            _id = clean(r.get("id"))
            name = clean(r.get("name"))

            if _id:
                ids.add(_id)
            if name:
                names.add(name)
                names_lower.add(name.lower())

            if _id and name:
                id_to_names[_id].add(name)
                name_to_ids[name].add(_id)

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    multi_name_ids = {
        k: sorted(v) for k, v in id_to_names.items() if len(v) > 1
    }
    multi_id_names = {
        k: sorted(v) for k, v in name_to_ids.items() if len(v) > 1
    }

    # ========================================================
    # GROUP BY RESTAURANT (الجزء الأول من الاسم)
    # ========================================================

    brand_to_names = defaultdict(set)      # key -> unique full names
    brand_to_ids = defaultdict(set)        # key -> ids (الفروع)
    brand_variants = defaultdict(Counter)  # key -> أشكال كتابة الاسم

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

        # لو اتكرر الـ display لمطعمين بمفاتيح مختلفة، نخليه فريد
        if display in grouped:
            display = f"{display} ({k})"

        grouped[display] = {
            "unique_names": len(brand_names),
            "unique_ids": len(brand_to_ids[k]),
            "name_variants": sorted(brand_variants[k].keys()),
            "names": sorted(brand_names),
        }

    merged = {b: g for b, g in grouped.items() if g["unique_names"] > 1}

    result = {
        "files": len(files),
        "total_records": total_records,
        "unique_id": len(ids),
        "unique_name": len(names),
        "unique_name_case_insensitive": len(names_lower),
        "unique_restaurants_after_grouping": len(grouped),
        "restaurants_with_multiple_branches": len(merged),
    }

    print("\n" + "=" * 60)
    for k, v in result.items():
        print(f"{k:<38}: {v}")
    print("=" * 60)

    print(f"\nids with multiple names : {len(multi_name_ids)}")
    print(f"names with multiple ids : {len(multi_id_names)}")

    # ========================================================
    # PRINT: المطاعم اللي اتجمعت في مطعم واحد
    # ========================================================

    print("\n" + "#" * 60)
    print(f"RESTAURANTS MERGED INTO ONE ({len(merged)})")
    print("#" * 60)

    for brand, info in sorted(
        merged.items(), key=lambda x: -x[1]["unique_names"]
    ):
        print(
            f"\n{brand}  "
            f"[{info['unique_names']} names | {info['unique_ids']} ids]"
        )

        if len(info["name_variants"]) > 1:
            print(f"  spelling variants: {info['name_variants']}")

        for n in info["names"]:
            print(f"    - {n}")

    result["ids_with_multiple_names"] = multi_name_ids
    result["names_with_multiple_ids"] = multi_id_names
    result["restaurants_grouped"] = grouped

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()