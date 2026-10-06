import os
import json
from collections import defaultdict

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

    result = {
        "files": len(files),
        "total_records": total_records,
        "unique_id": len(ids),
        "unique_name": len(names),
        "unique_name_case_insensitive": len(names_lower),
    }

    print("\n" + "=" * 60)
    for k, v in result.items():
        print(f"{k:<30}: {v}")
    print("=" * 60)

    print(f"\nids with multiple names : {len(multi_name_ids)}")
    print(f"names with multiple ids : {len(multi_id_names)}")

    # preview of the first 10 examples
    if multi_name_ids:
        print("\nExamples: id -> multiple names")
        for k, v in list(multi_name_ids.items())[:10]:
            print(f"  {k}: {v}")

    if multi_id_names:
        print("\nExamples: name -> multiple ids")
        for k, v in list(multi_id_names.items())[:10]:
            print(f"  {k}: {v}")

    result["ids_with_multiple_names"] = multi_name_ids
    result["names_with_multiple_ids"] = multi_id_names

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()