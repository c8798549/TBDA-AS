import os
import json
import boto3
from collections import Counter
from botocore.config import Config
from typing import List, Dict, Any, Tuple


# ============================================================
# 1. Setup R2 client
# ============================================================
def get_r2_client():
    """Create a boto3 S3-compatible client for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


BUCKET = os.environ["CF_R2_BUCKET_NAME"]
PREFIX = "merged-restaurant-info/year=2025/month=09/day=17/"


# ============================================================
# 2. List all JSON files under the prefix
# ============================================================
def list_json_files(client, prefix: str) -> List[str]:
    """Return all JSON object keys under the given prefix."""
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".json"):
                keys.append(key)
    return keys


# ============================================================
# 3. Read a single JSON file from R2
# ============================================================
def read_json_from_r2(client, key: str) -> Any:
    """Download and parse a JSON object from R2."""
    resp = client.get_object(Bucket=BUCKET, Key=key)
    body = resp["Body"].read().decode("utf-8")
    return json.loads(body)


# ============================================================
# 4. Extract records from any JSON shape
# ============================================================
def extract_records(data: Any) -> List[Dict[str, Any]]:
    """
    Supports multiple JSON shapes:
    - list of dicts
    - {"data": [...]}, {"records": [...]}, {"items": [...]}, {"results": [...]}
    - a single dict -> returned as a one-element list
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("data", "records", "items", "results"):
            if k in data and isinstance(data[k], list):
                return [r for r in data[k] if isinstance(r, dict)]
        return [data]
    return []


# ============================================================
# 5. Resolve the unique id key for a record
# ============================================================
UNIQUE_ID_CANDIDATES = ("unique_id", "id", "restaurant_id", "place_id")


def get_unique_id(record: Dict[str, Any]) -> str | None:
    """Return the unique id value from a record, or None if not found."""
    for k in UNIQUE_ID_CANDIDATES:
        if k in record and record[k] not in (None, ""):
            return str(record[k])
    return None


# ============================================================
# 6. Main workflow
# ============================================================
def main():
    client = get_r2_client()

    # --- (a) List all files ---
    keys = list_json_files(client, PREFIX)
    print(f"📁 Files under {PREFIX}: {len(keys)}")
    for k in keys:
        print(f"   - {k}")

    # --- (b) Read each file and count its records ---
    per_file_counts: List[Tuple[str, int]] = []
    all_records: List[Dict[str, Any]] = []
    failed_files: List[Tuple[str, str]] = []

    for key in keys:
        try:
            data = read_json_from_r2(client, key)
            records = extract_records(data)
            per_file_counts.append((key, len(records)))
            all_records.extend(records)
        except Exception as e:
            failed_files.append((key, str(e)))

    print("\n📊 Records count per file:")
    for k, c in per_file_counts:
        print(f"   {k} -> {c} records")

    if failed_files:
        print("\n⚠️ Files that failed to read:")
        for k, err in failed_files:
            print(f"   {k}: {err}")

    # --- (c) Total before deduplication ---
    total_before = len(all_records)
    print(f"\n🧮 Total records (before dedup): {total_before}")

    # --- (d) Remove duplicates based on unique id ---
    unique_map: Dict[str, Dict[str, Any]] = {}
    missing_id_count = 0

    for rec in all_records:
        uid = get_unique_id(rec)
        if uid is None:
            missing_id_count += 1
            continue
        # Keep the first occurrence for each unique id
        if uid not in unique_map:
            unique_map[uid] = rec

    total_after = len(unique_map)
    duplicates = total_before - total_after - missing_id_count

    print(f"\n✅ Unique ids count: {total_after}")
    print(f"♻️ Duplicates removed: {duplicates}")
    print(f"❓ Records without unique id: {missing_id_count}")

    # --- (e) Show top duplicated ids (optional but useful) ---
    print("\n🔍 Top 10 most duplicated unique ids:")
    id_counter = Counter(
        uid for rec in all_records
        if (uid := get_unique_id(rec)) is not None
    )
    for uid, cnt in id_counter.most_common(10):
        if cnt > 1:
            print(f"   {uid} -> {cnt} times")

    # --- (f) Final report ---
    print("\n" + "=" * 50)
    print("📌 Final Report")
    print("=" * 50)
    print(f"Files count              : {len(keys)}")
    print(f"Total before dedup       : {total_before}")
    print(f"Total after dedup        : {total_after}")
    print(f"Duplicates removed       : {duplicates}")
    print(f"Records without unique_id: {missing_id_count}")


if __name__ == "__main__":
    main()