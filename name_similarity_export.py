
import os
import re
import json
import csv
import unicodedata
from itertools import combinations
from collections import defaultdict

import boto3
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

SIM_FUZZY_MIN = 0.75
SIM_REORDER_MIN = 0.75
SIM_MAX_LEN_DIFF = 0.35
MIN_PREFIX_LENGTH = 4

# Compare records even when their IDs are identical.
COMPARE_SAME_ID = True

SEP_RE = re.compile(r"\s*(?:,|\(|\s[-–—]\s)\s*")
APOSTROPHES = re.compile(r"[\\'`´‘’ʼ′ʻ]")


# ============================================================
# R2 CLIENT
# ============================================================

required_env = [
    "CF_R2_ENDPOINT_URL",
    "CF_R2_ACCESS_KEY_ID",
    "CF_R2_SECRET_ACCESS_KEY",
    "CF_R2_BUCKET_NAME",
]

missing_env = [
    key for key in required_env
    if not os.environ.get(key)
]

if missing_env:
    raise RuntimeError(
        "Missing environment variables: "
        + ", ".join(missing_env)
    )

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
    region_name="auto",
)

BUCKET = os.environ["CF_R2_BUCKET_NAME"]


# ============================================================
# HELPERS
# ============================================================

def load_records(key):
    """
    Use the original loading logic:
    - JSON list -> return the list.
    - Dictionary containing data/records/items/results list -> return it.
    - Other dictionary -> return it as one record.
    - Other JSON types -> return an empty list.

    No recursive extraction.
    """
    response = s3.get_object(Bucket=BUCKET, Key=key)

    body = response["Body"].read().decode("utf-8-sig")
    data = json.loads(body)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for field in ("data", "records", "items", "results"):
            if isinstance(data.get(field), list):
                return data[field]

        return [data]

    return []


def clean(value):
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def normalize_text(value):
    if value is None:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    value = APOSTROPHES.sub("", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalize_separators(value):
    """Normalize punctuation and whitespace as separators."""
    value = normalize_text(value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def squash(value):
    """Remove spaces and punctuation for same-text comparison."""
    value = normalize_text(value)
    return "".join(ch for ch in value if ch.isalnum())


def tokens(value):
    value = normalize_text(value)
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def first_segment(value):
    """
    Get the first name segment before a comma or a spaced hyphen.
    Opening parentheses also mark an alternate segment.
    """
    value = str(value or "").strip()
    return SEP_RE.split(value, maxsplit=1)[0].strip()


def add_reason(reasons, reason):
    if reason not in reasons:
        reasons.append(reason)


# ============================================================
# NAME MATCHING
# ============================================================

def get_name_reasons(name_a, name_b):
    """
    Find every applicable name-based candidate reason.
    No grouping or merging takes place.
    """
    raw_a = clean(name_a)
    raw_b = clean(name_b)

    if not raw_a or not raw_b:
        return [], {
            "name_similarity": None,
            "reordered_similarity": None,
        }

    reasons = []

    a = normalize_text(raw_a)
    b = normalize_text(raw_b)

    compact_a = squash(raw_a)
    compact_b = squash(raw_b)

    sep_a = normalize_separators(raw_a)
    sep_b = normalize_separators(raw_b)

    token_a = tokens(raw_a)
    token_b = tokens(raw_b)

    scores = {
        "name_similarity": None,
        "reordered_similarity": None,
    }

    # 1. Identical original text: this is the only reason.
    if raw_a == raw_b:
        return ["same_text"], scores

    # 2. Case-only difference.
    if a == b:
        add_reason(reasons, "case_insensitive")

    # 3. Same text after removing punctuation and spaces.
    # Different punctuation/separator forms are classified here.
    if compact_a == compact_b:
        add_reason(reasons, "separator")

    # 4. Same normalized words and separators, but other differences.
    if (
        sep_a
        and sep_a == sep_b
        and a != b
        and "case_insensitive" not in reasons
        and "separator" not in reasons
    ):
        add_reason(reasons, "separator")

    # 5. Same words in a different order.
    if (
        len(token_a) > 1
        and len(token_b) > 1
        and sorted(token_a) == sorted(token_b)
        and token_a != token_b
    ):
        add_reason(reasons, "reordered")

    # 6. Prefix: one normalized full name starts with the other.
    if sep_a and sep_b and sep_a != sep_b:
        shorter, longer = sorted([sep_a, sep_b], key=len)

        if (
            len(shorter.replace(" ", "")) >= MIN_PREFIX_LENGTH
            and longer.startswith(shorter)
        ):
            add_reason(reasons, "prefix")

    # 7. Fuzzy spelling similarity.
    if sep_a and sep_b and sep_a != sep_b:
        max_len = max(len(sep_a), len(sep_b))
        length_difference = abs(len(sep_a) - len(sep_b)) / max_len

        if length_difference <= SIM_MAX_LEN_DIFF:
            similarity = fuzz.ratio(sep_a, sep_b) / 100

            if similarity >= SIM_FUZZY_MIN:
                add_reason(reasons, "fuzzy")
                scores["name_similarity"] = round(similarity, 4)

    # 8. Approximate reordered-word similarity.
    if len(token_a) > 1 and len(token_b) > 1:
        sorted_a = " ".join(sorted(token_a))
        sorted_b = " ".join(sorted(token_b))

        if sorted_a != sorted_b:
            max_len = max(len(sorted_a), len(sorted_b))
            length_difference = (
                abs(len(sorted_a) - len(sorted_b)) / max_len
            )

            if length_difference <= SIM_MAX_LEN_DIFF:
                similarity = fuzz.ratio(sorted_a, sorted_b) / 100

                if similarity >= SIM_REORDER_MIN:
                    add_reason(reasons, "reordered")
                    scores["reordered_similarity"] = round(
                        similarity, 4
                    )

    return reasons, scores


# ============================================================
# READ ALL ORIGINAL RECORDS
# ============================================================

def read_all_records():
    files = []

    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=BUCKET,
        Prefix=R2_PREFIX,
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                files.append(obj["Key"])

    files.sort()

    print(f"JSON files found: {len(files)}")

    records = []
    invalid_files = []

    for file_number, key in enumerate(files, start=1):
        try:
            file_records = load_records(key)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            invalid_files.append((key, str(exc)))
            continue

        print(
            f"[{file_number}/{len(files)}] "
            f"{key} -> {len(file_records)} records"
        )

        for record_number, record in enumerate(file_records):
            if not isinstance(record, dict):
                print(
                    f"WARNING: non-dictionary record in {key}, "
                    f"index {record_number}; skipped"
                )
                continue

            # Keep original data unchanged in a separate field.
            records.append({
                "record_index": len(records),
                "source_file": key,
                "source_record_index": record_number,
                "data": record,
            })

    print(f"Invalid JSON files: {len(invalid_files)}")

    if invalid_files:
        for key, error in invalid_files:
            print(f"Invalid JSON: {key}: {error}")

        raise RuntimeError(
            "Some input files could not be read. Export stopped."
        )

    print(f"Extracted source records: {len(records)}")

    if len(records) != EXPECTED_RECORD_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_RECORD_COUNT} records, "
            f"but loaded {len(records)}. "
            "Export stopped. Verify the prefix and record structure."
        )

    return records


# ============================================================
# CANDIDATE PAIR COLLECTION
# ============================================================

def discover_candidates(records):
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

        pair = (min(i, j), max(i, j))

        if pair not in candidates:
            candidates[pair] = {
                "reasons": set(),
                "name_similarity": None,
                "reordered_similarity": None,
            }

        candidate = candidates[pair]
        candidate["reasons"].add(reason)

        if score is not None:
            field = (
                "reordered_similarity"
                if reason == "reordered"
                else "name_similarity"
            )

            old_score = candidate[field]

            if old_score is None or score > old_score:
                candidate[field] = score

    # --------------------------------------------------------
    # A. Index all original records by raw name.
    # Duplicate records are retained as separate records.
    # --------------------------------------------------------

    name_to_indices = defaultdict(list)

    for index, item in enumerate(records):
        name = clean(item["data"].get("name"))

        if name:
            name_to_indices[name].append(index)

    unique_names = list(name_to_indices)

    print(f"Unique raw names: {len(unique_names)}")
    print("Checking distinct name pairs...")

    # Compare all distinct raw names; no first-character blocking.
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

    # Exact raw-name duplicates.
    for name, indices in name_to_indices.items():
        for i, j in combinations(indices, 2):
            add_candidate(i, j, "same_text")

    # --------------------------------------------------------
    # B. BranchName candidate matching.
    #
    # Match normalized branchName against another record's name.
    # Also check prefix relationships in either direction.
    # --------------------------------------------------------

    normalized_name_index = defaultdict(list)

    for index, item in enumerate(records):
        name = normalize_separators(item["data"].get("name"))

        if name:
            normalized_name_index[name].append(index)

    # Exact normalized branchName/name matches.
    for i, item in enumerate(records):
        branch_name = item["data"].get("branchName")
        branch_key = normalize_separators(branch_name)

        if not branch_key:
            continue

        for j in normalized_name_index.get(branch_key, []):
            if i != j:
                add_candidate(i, j, "branchname")

    # Prefix matching for branchName against restaurant names.
    # This finds branch names such as "Yuzu - Salmiya"
    # when another restaurant is named "Yuzu".
    name_entries = [
        (normalize_separators(name), indices)
        for name, indices in name_to_indices.items()
        if normalize_separators(name)
    ]

    for i, item in enumerate(records):
        branch_key = normalize_separators(
            item["data"].get("branchName")
        )

        if not branch_key:
            continue

        for name_key, indices in name_entries:
            if branch_key == name_key:
                continue

            shorter, longer = sorted(
                [branch_key, name_key],
                key=len,
            )

            if (
                len(shorter.replace(" ", "")) >= MIN_PREFIX_LENGTH
                and longer.startswith(shorter)
            ):
                for j in indices:
                    if i != j:
                        add_candidate(i, j, "branchname")

    # --------------------------------------------------------
    # C. Build export rows.
    # Full original records are preserved in JSON.
    # --------------------------------------------------------

    rows = []

    for (i, j), candidate in candidates.items():
        item_a = records[i]
        item_b = records[j]

        rows.append({
            "record_a_index": i,
            "record_b_index": j,
            "match_reasons": " | ".join(
                sorted(candidate["reasons"])
            ),
            "name_similarity": candidate["name_similarity"],
            "reordered_similarity": candidate["reordered_similarity"],
            "source_file_a": item_a["source_file"],
            "source_file_b": item_b["source_file"],
            "source_record_index_a": item_a["source_record_index"],
            "source_record_index_b": item_b["source_record_index"],
            "record_a": item_a["data"],
            "record_b": item_b["data"],
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
    # JSON: nested original records, with every original field.
    with open(JSON_FILE, "w", encoding="utf-8") as file:
        json.dump(
            rows,
            file,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    # CSV: dynamically include every field found in either record.
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
        "source_record_index_a",
        "source_record_index_b",
    ]

    columns = list(metadata_fields)

    for field in sorted(all_fields):
        columns.extend([f"a_{field}", f"b_{field}"])

    with open(
        CSV_FILE,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()

        for row in rows:
            flat_row = {
                field: row.get(field)
                for field in metadata_fields
            }

            for field in all_fields:
                value_a = row["record_a"].get(field)
                value_b = row["record_b"].get(field)

                flat_row[f"a_{field}"] = (
                    json.dumps(value_a, ensure_ascii=False)
                    if isinstance(value_a, (dict, list))
                    else value_a
                )

                flat_row[f"b_{field}"] = (
                    json.dumps(value_b, ensure_ascii=False)
                    if isinstance(value_b, (dict, list))
                    else value_b
                )

            writer.writerow(flat_row)

    print(f"Candidate pairs: {len(rows)}")
    print(f"CSV saved: {CSV_FILE}")
    print(f"JSON saved: {JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

def main():
    records = read_all_records()

    print("Original record count validated.")
    print("No grouping, merging, or connected components.")

    rows = discover_candidates(records)
    export_results(rows)


if __name__ == "__main__":
    main()
