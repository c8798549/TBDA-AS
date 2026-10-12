
import os
import re
import json
import unicodedata
from itertools import combinations
from collections import defaultdict

import boto3
import pandas as pd
from rapidfuzz import fuzz


# ============================================================
# CONFIG
# ============================================================

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

CSV_FILE = "restaurant_grouping_review.csv"
JSON_FILE = "restaurant_grouping_review.json"

EXPECTED_RECORD_COUNT = 4391

FUZZY_THRESHOLD = 70
REORDERED_THRESHOLD = 75
MIN_PREFIX_LENGTH = 4

# True = compare records even if they have the same ID.
COMPARE_SAME_ID = True


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    return value.casefold().strip()


def normalize_separators(value):
    """Replace punctuation and separators with spaces."""
    value = clean_text(value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def remove_separators(value):
    """Remove spaces and punctuation."""
    return "".join(
        ch for ch in clean_text(value)
        if ch.isalnum()
    )


def get_tokens(value):
    normalized = normalize_separators(value)
    return normalized.split() if normalized else []


# ============================================================
# NAME MATCHING RULES
# ============================================================

def get_name_reasons(name_a, name_b):
    """
    Return all applicable candidate reasons and their scores.

    No grouping or merging happens here.
    """
    raw_a = "" if name_a is None else str(name_a).strip()
    raw_b = "" if name_b is None else str(name_b).strip()

    if not raw_a or not raw_b:
        return [], {}

    a = clean_text(raw_a)
    b = clean_text(raw_b)

    sep_a = normalize_separators(a)
    sep_b = normalize_separators(b)

    compact_a = remove_separators(a)
    compact_b = remove_separators(b)

    tokens_a = get_tokens(a)
    tokens_b = get_tokens(b)

    reasons = []
    scores = {
        "name_similarity": None,
        "reordered_similarity": None,
    }

    # 1. Exactly identical original names.
    # This is the only reason for an exact raw-name match.
    if raw_a == raw_b:
        return ["same_text"], scores

    # 2. Case-only difference.
    if a == b:
        reasons.append("case_insensitive")

    # 3. Separator/punctuation/spacing difference.
    # Examples: Pizza-House / Pizza House, Yuzu! / Yuzu
    if (
        compact_a == compact_b
        and a != b
    ):
        reasons.append("separator")

    # Also recognize separator normalization when the compact
    # comparison is not sufficient for a particular Unicode case.
    if (
        sep_a
        and sep_a == sep_b
        and a != b
        and "separator" not in reasons
        and "case_insensitive" not in reasons
    ):
        reasons.append("separator")

    # 4. Exact reordered words.
    if (
        len(tokens_a) > 1
        and len(tokens_b) > 1
        and sorted(tokens_a) == sorted(tokens_b)
        and tokens_a != tokens_b
    ):
        reasons.append("reordered")

    # 5. Prefix candidates.
    if sep_a and sep_b and sep_a != sep_b:
        shorter, longer = sorted([sep_a, sep_b], key=len)

        if (
            len(shorter) >= MIN_PREFIX_LENGTH
            and longer.startswith(shorter)
        ):
            reasons.append("prefix")

    # 6. Fuzzy spelling similarity.
    if sep_a and sep_b and sep_a != sep_b:
        similarity = fuzz.ratio(sep_a, sep_b)
        scores["name_similarity"] = round(similarity, 2)

        if similarity >= FUZZY_THRESHOLD:
            reasons.append("fuzzy")

    # 7. Approximate reordered words.
    if (
        len(tokens_a) > 1
        and len(tokens_b) > 1
    ):
        sorted_a = " ".join(sorted(tokens_a))
        sorted_b = " ".join(sorted(tokens_b))

        reordered_similarity = fuzz.ratio(sorted_a, sorted_b)
        scores["reordered_similarity"] = round(
            reordered_similarity, 2
        )

        if (
            reordered_similarity >= REORDERED_THRESHOLD
            and tokens_a != tokens_b
            and "reordered" not in reasons
        ):
            reasons.append("reordered")

    return reasons, scores


# ============================================================
# READ ORIGINAL RECORDS FROM R2
# ============================================================

def get_r2_client():
    required = [
        "CF_R2_ENDPOINT_URL",
        "CF_R2_ACCESS_KEY_ID",
        "CF_R2_SECRET_ACCESS_KEY",
        "CF_R2_BUCKET_NAME",
    ]

    missing = [key for key in required if not os.environ.get(key)]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )

    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def extract_records(obj, source_file, path="$"):
    """
    Extract restaurant-like records recursively.

    Each extracted record retains all its original fields.
    No records are deduplicated or grouped.
    """
    records = []

    if isinstance(obj, dict):
        has_name = obj.get("name") is not None

        has_identifier = any(
            obj.get(field) is not None
            for field in (
                "id",
                "restaurantSlug",
                "branchId",
                "branchName",
            )
        )

        if has_name and has_identifier:
            records.append({
                "data": dict(obj),
                "source_file": source_file,
                "source_path": path,
            })
            return records

        for key, value in obj.items():
            records.extend(
                extract_records(
                    value,
                    source_file,
                    f"{path}.{key}",
                )
            )

    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            records.extend(
                extract_records(
                    item,
                    source_file,
                    f"{path}[{index}]",
                )
            )

    return records


def load_records():
    client = get_r2_client()
    bucket = os.environ["CF_R2_BUCKET_NAME"]

    records = []
    json_files = 0
    invalid_files = []

    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=R2_PREFIX,
    ):
        for item in page.get("Contents", []):
            key = item["Key"]

            if not key.lower().endswith(".json"):
                continue

            json_files += 1

            response = client.get_object(
                Bucket=bucket,
                Key=key,
            )

            try:
                content = response["Body"].read().decode("utf-8-sig")
                data = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                invalid_files.append({
                    "file": key,
                    "error": str(exc),
                })
                continue

            records.extend(extract_records(data, key))

    print(f"JSON files found: {json_files}")
    print(f"Invalid JSON files: {len(invalid_files)}")
    print(f"Extracted source records: {len(records)}")

    if invalid_files:
        for item in invalid_files:
            print(f"Invalid file: {item['file']} - {item['error']}")

        raise RuntimeError(
            "Some JSON files could not be read. "
            "Fix or inspect them before exporting."
        )

    if len(records) != EXPECTED_RECORD_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_RECORD_COUNT} records, "
            f"but extracted {len(records)}. "
            "Export stopped to prevent using an incomplete or "
            "unexpected record set. Verify the prefix and JSON schema."
        )

    for index, record in enumerate(records):
        record["record_index"] = index

    return records


# ============================================================
# PAIRWISE CANDIDATE DISCOVERY
# ============================================================

def discover_candidates(records):
    """
    Return possible record pairs with all applicable reasons.
    Does not merge records or assign final groups.
    """
    candidates = {}

    def add_candidate(i, j, reason, score=None):
        if i == j:
            return

        record_a = records[i]["data"]
        record_b = records[j]["data"]

        if not COMPARE_SAME_ID:
            id_a = record_a.get("id")
            id_b = record_b.get("id")

            if (
                id_a is not None
                and id_b is not None
                and str(id_a) == str(id_b)
            ):
                return

        pair_key = (min(i, j), max(i, j))

        if pair_key not in candidates:
            candidates[pair_key] = {
                "reasons": set(),
                "name_similarity": None,
                "reordered_similarity": None,
            }

        candidate = candidates[pair_key]
        candidate["reasons"].add(reason)

        if score is not None:
            score_field = (
                "reordered_similarity"
                if reason == "reordered"
                else "name_similarity"
            )

            previous = candidate[score_field]

            if previous is None or score > previous:
                candidate[score_field] = score

    # --------------------------------------------------------
    # A. Compare distinct raw names.
    # Identical-name records are handled separately below.
    # --------------------------------------------------------

    name_to_indices = defaultdict(list)

    for index, record in enumerate(records):
        name = record["data"].get("name")

        if name is not None and str(name).strip():
            name_to_indices[str(name).strip()].append(index)

    unique_names = list(name_to_indices)

    print(f"Unique raw names: {len(unique_names)}")
    print("Comparing unique name pairs...")

    for name_a, name_b in combinations(unique_names, 2):
        reasons, scores = get_name_reasons(name_a, name_b)

        if not reasons:
            continue

        for i in name_to_indices[name_a]:
            for j in name_to_indices[name_b]:
                for reason in reasons:
                    score = None

                    if reason == "fuzzy":
                        score = scores["name_similarity"]
                    elif reason == "reordered":
                        score = scores["reordered_similarity"]

                    add_candidate(i, j, reason, score)

    # --------------------------------------------------------
    # B. Identical raw names.
    # Each distinct pair of records is retained.
    # --------------------------------------------------------

    for name, indices in name_to_indices.items():
        for i, j in combinations(indices, 2):
            add_candidate(i, j, "same_text")

    # --------------------------------------------------------
    # C. BranchName matches another record's name.
    # Matching is case-insensitive and separator-normalized.
    # Check both directions naturally by checking every record.
    # --------------------------------------------------------

    normalized_name_index = defaultdict(list)

    for index, record in enumerate(records):
        name_key = normalize_separators(
            record["data"].get("name")
        )

        if name_key:
            normalized_name_index[name_key].append(index)

    for i, record in enumerate(records):
        branch_name = record["data"].get("branchName")
        branch_key = normalize_separators(branch_name)

        if not branch_key:
            continue

        for j in normalized_name_index.get(branch_key, []):
            if i != j:
                add_candidate(i, j, "branchname")

    # --------------------------------------------------------
    # D. Build output.
    # Keep every original field from both records.
    # --------------------------------------------------------

    rows = []

    for (i, j), candidate in candidates.items():
        record_a = records[i]
        record_b = records[j]

        rows.append({
            "record_a_index": i,
            "record_b_index": j,
            "match_reasons": " | ".join(
                sorted(candidate["reasons"])
            ),
            "name_similarity": candidate["name_similarity"],
            "reordered_similarity": candidate["reordered_similarity"],
            "source_file_a": record_a["source_file"],
            "source_file_b": record_b["source_file"],
            "source_path_a": record_a["source_path"],
            "source_path_b": record_b["source_path"],
            "record_a": record_a["data"],
            "record_b": record_b["data"],
        })

    rows.sort(
        key=lambda row: (
            str(row["record_a"].get("name") or "").casefold(),
            str(row["record_b"].get("name") or "").casefold(),
            row["record_a_index"],
            row["record_b_index"],
        )
    )

    return rows


# ============================================================
# EXPORT CSV + JSON
# ============================================================

def export_results(rows):
    # JSON keeps the original records nested and intact.
    with open(JSON_FILE, "w", encoding="utf-8") as file:
        json.dump(
            rows,
            file,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    # CSV has all original fields from both records.
    # Dynamic columns prevent dropping unknown attributes.
    all_fields = set()

    for row in rows:
        all_fields.update(row["record_a"].keys())
        all_fields.update(row["record_b"].keys())

    metadata_fields = [
        "record_a_index",
        "record_b_index",
        "match_reasons",
        "name_similarity",
        "reordered_similarity",
        "source_file_a",
        "source_file_b",
        "source_path_a",
        "source_path_b",
    ]

    columns = list(metadata_fields)

    for field in sorted(all_fields):
        columns.append(f"a_{field}")
        columns.append(f"b_{field}")

    csv_rows = []

    for row in rows:
        flat_row = {
            key: row.get(key)
            for key in metadata_fields
        }

        for field in all_fields:
            flat_row[f"a_{field}"] = row["record_a"].get(field)
            flat_row[f"b_{field}"] = row["record_b"].get(field)

        csv_rows.append(flat_row)

    df = pd.DataFrame(csv_rows, columns=columns)
    df.to_csv(CSV_FILE, index=False, encoding="utf-8-sig")

    print(f"Candidate pairs: {len(rows)}")
    print(f"CSV saved: {CSV_FILE}")
    print(f"JSON saved: {JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

def main():
    records = load_records()

    print(f"Validated source record count: {len(records)}")
    print("No grouping or merging will be performed.")

    candidates = discover_candidates(records)
    export_results(candidates)


if __name__ == "__main__":
    main()
