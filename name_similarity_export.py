
import os
import re
import json
import csv
import itertools
import unicodedata
from collections import Counter, defaultdict

import boto3
from rapidfuzz import fuzz


# ============================================================
# CONFIG
# ============================================================

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

OUTPUT_DIR = "output"

SIM_FUZZY_MIN = 75
SIM_REORDER_MIN = 75
SIM_MAX_LEN_DIFF = 0.35
MIN_PREFIX_LENGTH = 4

# Keep potential matches even when the IDs are identical.
COMPARE_SAME_ID = True


# ============================================================
# R2 CONNECTION
# ============================================================

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)

BUCKET = os.environ["CF_R2_BUCKET_NAME"]

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# JSON HELPERS
# ============================================================

def save_json(filename, data):
    path = os.path.join(OUTPUT_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"Saved: {path}")


def load_records(key):
    """
    Read restaurant records using the original reader.
    Do not recursively extract nested dictionaries.
    """
    body = (
        s3.get_object(Bucket=BUCKET, Key=key)["Body"]
        .read()
        .decode("utf-8")
    )

    data = json.loads(body)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for field in ("data", "records", "items", "results"):
            if isinstance(data.get(field), list):
                return data[field]

        return [data]

    return []


# ============================================================
# COLLECT ALL SOURCE RECORDS
# ============================================================

def read_all_records():
    records = []
    files_read = 0
    invalid_records = 0

    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=BUCKET,
        Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]

            if not key.lower().endswith(".json"):
                continue

            # Ignore directory-marker objects.
            if key.endswith("/"):
                continue

            file_records = load_records(key)
            files_read += 1

            for source_index, record in enumerate(file_records):
                if not isinstance(record, dict):
                    invalid_records += 1
                    print(
                        f"WARNING: Skipping non-dict record "
                        f"in {key}, index {source_index}"
                    )
                    continue

                # Preserve the complete original record.
                records.append({
                    "_source_file": key,
                    "_source_index": source_index,
                    "data": record
                })

            print(
                f"Read {key}: {len(file_records)} source records"
            )

    print("\n========== COLLECTION ==========")
    print(f"Files read: {files_read}")
    print(f"Valid source records: {len(records)}")
    print(f"Invalid non-dict records: {invalid_records}")

    return records, files_read, invalid_records


# ============================================================
# UNIQUE IDS AND UNIQUE RECORDS
# ============================================================

def export_unique_data(records):
    unique_ids = set()
    unique_records = []
    duplicate_id_count = 0
    missing_id_count = 0
    id_counts = Counter()

    for wrapper in records:
        record = wrapper["data"]
        record_id = record.get("id")

        if record_id is None or str(record_id).strip() == "":
            missing_id_count += 1
            continue

        # Normalize ID representation for duplicate detection.
        normalized_id = str(record_id).strip()
        unique_ids.add(normalized_id)
        id_counts[normalized_id] += 1

    seen_ids = set()

    for wrapper in records:
        record = wrapper["data"]
        record_id = record.get("id")

        if record_id is None or str(record_id).strip() == "":
            continue

        normalized_id = str(record_id).strip()

        if normalized_id in seen_ids:
            duplicate_id_count += 1
            continue

        seen_ids.add(normalized_id)

        # Keep every original field and its original values.
        unique_records.append({
            **record,
            "_source_file": wrapper["_source_file"],
            "_source_index": wrapper["_source_index"]
        })

    sorted_ids = sorted(unique_ids)

    save_json("all_source_records.json", [
        {
            "_source_file": wrapper["_source_file"],
            "_source_index": wrapper["_source_index"],
            **wrapper["data"]
        }
        for wrapper in records
    ])

    save_json("unique_ids.json", sorted_ids)
    save_json("unique_restaurants.json", unique_records)

    duplicate_ids = {
        record_id: count
        for record_id, count in id_counts.items()
        if count > 1
    }

    save_json("duplicate_id_counts.json", duplicate_ids)

    return {
        "unique_ids_count": len(sorted_ids),
        "unique_records_count": len(unique_records),
        "duplicate_records_removed": duplicate_id_count,
        "records_missing_id": missing_id_count,
        "duplicate_ids": duplicate_ids
    }


# ============================================================
# NAME NORMALIZATION
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    return " ".join(value.casefold().split())


def normalize_separators(value):
    value = normalize_text(value)

    # Treat punctuation/separators as spaces for comparison.
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def compact(value):
    return re.sub(r"\s+", "", normalize_separators(value))


def token_signature(value):
    return tuple(sorted(normalize_separators(value).split()))


def length_difference(a, b):
    longest = max(len(a), len(b), 1)
    return abs(len(a) - len(b)) / longest


# ============================================================
# NAME MATCH REASONS
# ============================================================

def get_name_reasons(name_a, name_b):
    if name_a is None or name_b is None:
        return []

    raw_a = str(name_a).strip()
    raw_b = str(name_b).strip()

    if not raw_a or not raw_b:
        return []

    reasons = set()

    # Exact raw match: same_text only.
    if raw_a == raw_b:
        return ["same_text"]

    norm_a = normalize_text(raw_a)
    norm_b = normalize_text(raw_b)

    # Case-only difference.
    if norm_a == norm_b:
        reasons.add("case_insensitive")

    sep_a = normalize_separators(raw_a)
    sep_b = normalize_separators(raw_b)

    # Punctuation or separator variation.
    if sep_a == sep_b:
        reasons.add("separator")

    # Same words in a different order.
    if (
        token_signature(raw_a) == token_signature(raw_b)
        and sep_a != sep_b
    ):
        reasons.add("reordered")

    compact_a = compact(raw_a)
    compact_b = compact(raw_b)

    # Prefix match.
    if min(len(compact_a), len(compact_b)) >= MIN_PREFIX_LENGTH:
        if (
            compact_a.startswith(compact_b)
            or compact_b.startswith(compact_a)
        ):
            reasons.add("prefix")

    # Fuzzy and token-order similarity.
    if length_difference(sep_a, sep_b) <= SIM_MAX_LEN_DIFF:
        if fuzz.ratio(sep_a, sep_b) >= SIM_FUZZY_MIN:
            reasons.add("fuzzy")

        if (
            fuzz.token_sort_ratio(sep_a, sep_b)
            >= SIM_REORDER_MIN
            and token_signature(raw_a) != token_signature(raw_b)
        ):
            reasons.add("reordered")

    return sorted(reasons)


# ============================================================
# PAIRWISE REVIEW - NO GROUPING
# ============================================================

def discover_candidates(records):
    """
    Return individual record pairs.
    No merging, no connected components, no group IDs.
    """
    names_to_indexes = defaultdict(list)
    name_values = {}

    for i, wrapper in enumerate(records):
        record = wrapper["data"]
        name = record.get("name")

        if name is None or not str(name).strip():
            continue

        raw_name = str(name).strip()
        names_to_indexes[raw_name].append(i)
        name_values[raw_name] = raw_name

    unique_names = list(names_to_indexes)
    pair_reasons = {}

    def add_pair(i, j, reason):
        if i == j:
            return

        if not COMPARE_SAME_ID:
            id_a = records[i]["data"].get("id")
            id_b = records[j]["data"].get("id")

            if (
                id_a is not None
                and id_b is not None
                and str(id_a).strip() == str(id_b).strip()
            ):
                return

        if i > j:
            i, j = j, i

        pair_reasons.setdefault((i, j), set()).add(reason)

    # Compare unique restaurant names first.
    for name_a, name_b in itertools.combinations(unique_names, 2):
        reasons = get_name_reasons(name_a, name_b)

        if not reasons:
            continue

        for i in names_to_indexes[name_a]:
            for j in names_to_indexes[name_b]:
                for reason in reasons:
                    add_pair(i, j, reason)

    # Identical raw names also produce record-level pairs.
    for name, indexes in names_to_indexes.items():
        if len(indexes) > 1:
            for i, j in itertools.combinations(indexes, 2):
                add_pair(i, j, "same_text")

    # Compare branchName against other records' name values.
    name_index = defaultdict(list)

    for i, wrapper in enumerate(records):
        name = wrapper["data"].get("name")

        if name is not None and str(name).strip():
            name_index[normalize_separators(name)].append(i)

    normalized_names = list(name_index.keys())

    for i, wrapper in enumerate(records):
        branch_name = wrapper["data"].get("branchName")

        if branch_name is None or not str(branch_name).strip():
            continue

        branch_norm = normalize_separators(branch_name)

        if not branch_norm:
            continue

        # Exact normalized branchName/name match.
        for j in name_index.get(branch_norm, []):
            if i != j:
                add_pair(i, j, "branchname")

        # Prefix branchName/name match.
        if len(branch_norm.replace(" ", "")) >= MIN_PREFIX_LENGTH:
            for name_norm in normalized_names:
                if name_norm == branch_norm:
                    continue

                shorter, longer = sorted(
                    [branch_norm, name_norm],
                    key=len
                )

                if (
                    len(shorter.replace(" ", "")) >= MIN_PREFIX_LENGTH
                    and longer.startswith(shorter)
                ):
                    for j in name_index[name_norm]:
                        if i != j:
                            add_pair(i, j, "branchname")

    # Export a separate row for each candidate pair.
    results = []

    for (i, j), reasons in sorted(pair_reasons.items()):
        a = records[i]
        b = records[j]

        results.append({
            "source_index_a": i,
            "source_index_b": j,
            "source_file_a": a["_source_file"],
            "source_file_b": b["_source_file"],
            "id_a": a["data"].get("id"),
            "id_b": b["data"].get("id"),
            "name_a": a["data"].get("name"),
            "name_b": b["data"].get("name"),
            "branchName_a": a["data"].get("branchName"),
            "branchName_b": b["data"].get("branchName"),
            "match_reasons": sorted(reasons),
            "record_a": a["data"],
            "record_b": b["data"]
        })

    print(f"\nPotential record pairs: {len(results)}")
    return results


# ============================================================
# EXPORT PAIRWISE REVIEW JSON AND CSV
# ============================================================

def export_review(results):
    save_json("restaurant_grouping_review.json", results)

    csv_path = os.path.join(
        OUTPUT_DIR,
        "restaurant_grouping_review.csv"
    )

    # Keep every source field from both records.
    source_fields = set()

    for row in results:
        source_fields.update(row["record_a"].keys())
        source_fields.update(row["record_b"].keys())

    fixed_fields = [
        "source_index_a",
        "source_index_b",
        "source_file_a",
        "source_file_b",
        "id_a",
        "id_b",
        "name_a",
        "name_b",
        "branchName_a",
        "branchName_b",
        "match_reasons"
    ]

    dynamic_fields = (
        [f"a_{field}" for field in sorted(source_fields)]
        + [f"b_{field}" for field in sorted(source_fields)]
    )

    fieldnames = fixed_fields + dynamic_fields

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore"
        )
        writer.writeheader()

        for row in results:
            output_row = {
                key: row.get(key)
                for key in fixed_fields
            }

            output_row["match_reasons"] = json.dumps(
                row["match_reasons"],
                ensure_ascii=False
            )

            for field in source_fields:
                output_row[f"a_{field}"] = json.dumps(
                    row["record_a"].get(field),
                    ensure_ascii=False,
                    default=str
                ) if field in row["record_a"] else ""

                output_row[f"b_{field}"] = json.dumps(
                    row["record_b"].get(field),
                    ensure_ascii=False,
                    default=str
                ) if field in row["record_b"] else ""

            writer.writerow(output_row)

    print(f"Saved: {csv_path}")


# ============================================================
# MAIN
# ============================================================

def main():
    records, files_read, invalid_records = read_all_records()

    # 1. Save all source records and unique-ID outputs.
    unique_summary = export_unique_data(records)

    # 2. Generate review pairs from ALL source records, not deduplicated ones.
    # This preserves potential matches even when IDs are duplicated.
    review_results = discover_candidates(records)

    # 3. Save pairwise review files.
    export_review(review_results)

    # 4. Save collection summary.
    summary = {
        "r2_prefix": R2_PREFIX,
        "files_read": files_read,
        "source_records_loaded": len(records),
        "invalid_non_dict_records": invalid_records,
        "unique_ids_count": unique_summary["unique_ids_count"],
        "unique_records_count": unique_summary["unique_records_count"],
        "duplicate_records_removed_for_unique_export":
            unique_summary["duplicate_records_removed"],
        "records_missing_id": unique_summary["records_missing_id"],
        "duplicate_id_values_count":
            len(unique_summary["duplicate_ids"]),
        "potential_review_pairs": len(review_results),
        "grouping_performed": False,
        "records_merged": False
    }

    save_json("collection_summary.json", summary)

    print("\n========== FINISHED ==========")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
