
import os
import re
import json
import unicodedata
from itertools import combinations
from collections import defaultdict
from difflib import SequenceMatcher

import boto3
import pandas as pd
from rapidfuzz import fuzz, process


# ============================================================
# CONFIG
# ============================================================

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

CSV_FILE = "restaurant_grouping_review.csv"
JSON_FILE = "restaurant_grouping_review.json"

FUZZY_THRESHOLD = 70
REORDERED_THRESHOLD = 75
MIN_PREFIX_LENGTH = 4

# Set to True only if you want to compare records sharing the same ID.
# False skips those pairs because they may represent the same source entity.
COMPARE_SAME_ID = True

OUTPUT_FIELDS = [
    "id",
    "name",
    "restaurantSlug",
    "branchId",
    "branchName",
    "branchSlug",
    "branchUrl",
    "cuisineString",
    "shopCity",
]


# ============================================================
# NORMALIZATION
# ============================================================

def clean_text(value):
    """Normalize Unicode, lowercase, and trim whitespace."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    return value.casefold().strip()


def remove_separators(value):
    """
    Remove spaces and punctuation.
    Yuzu House, Yuzu-House and YuzuHouse become yuzuhouse.
    """
    value = clean_text(value)
    return "".join(ch for ch in value if ch.isalnum())


def normalize_separators(value):
    """
    Treat punctuation and separators as spaces.
    Pizza-House and Pizza House become pizza house.
    """
    value = clean_text(value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def tokens(value):
    value = normalize_separators(value)
    return value.split() if value else []


def get_reasons(name_a, name_b):
    """Return every applicable name-based candidate reason."""
    reasons = []
    raw_a = "" if name_a is None else str(name_a).strip()
    raw_b = "" if name_b is None else str(name_b).strip()

    a = clean_text(raw_a)
    b = clean_text(raw_b)

    if not a or not b:
        return reasons, {}

    compact_a = remove_separators(a)
    compact_b = remove_separators(b)

    separated_a = normalize_separators(a)
    separated_b = normalize_separators(b)

    # Exact match after normalization:
    # this is the only reason for identical names, including exact raw names.
    if compact_a == compact_b:
        if raw_a == raw_b:
            reasons.append("same_text")
        elif a == b:
            reasons.append("case_insensitive")
        else:
            reasons.append("same_text")

    # Separator variation, e.g. Pizza-House vs Pizza House.
    if (
        separated_a
        and separated_a == separated_b
        and a != b
        and "same_text" not in reasons
        and "case_insensitive" not in reasons
    ):
        reasons.append("separator")

    # Same words, different order.
    token_a = tokens(a)
    token_b = tokens(b)

    if (
        len(token_a) > 1
        and len(token_b) > 1
        and sorted(token_a) == sorted(token_b)
        and token_a != token_b
    ):
        reasons.append("reordered")

    # Prefix candidate.
    # This is a suggestion only, not a grouping decision.
    if separated_a and separated_b and separated_a != separated_b:
        shorter, longer = sorted(
            [separated_a, separated_b],
            key=len
        )
        if (
            len(shorter) >= MIN_PREFIX_LENGTH
            and longer.startswith(shorter)
        ):
            reasons.append("prefix")

    # Fuzzy spelling similarity.
    # Do not apply fuzzy matching to names already covered by exact rules.
    score = fuzz.ratio(separated_a, separated_b) if separated_a and separated_b else 0

    if (
        score >= FUZZY_THRESHOLD
        and separated_a != separated_b
        and "reordered" not in reasons
    ):
        reasons.append("fuzzy")

    # Similarity after sorting words, to find approximate reordered names.
    reordered_score = 0
    if token_a and token_b and sorted(token_a) != sorted(token_b):
        sorted_a = " ".join(sorted(token_a))
        sorted_b = " ".join(sorted(token_b))
        reordered_score = fuzz.ratio(sorted_a, sorted_b)

        if (
            reordered_score >= REORDERED_THRESHOLD
            and len(token_a) > 1
            and len(token_b) > 1
            and "reordered" not in reasons
        ):
            reasons.append("reordered")

    scores = {
        "name_similarity": round(score, 2),
        "reordered_similarity": round(reordered_score, 2),
    }

    return reasons, scores


# ============================================================
# READ RECORDS FROM R2
# ============================================================

def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def extract_records(obj, source_file):
    """
    Extract restaurant records from a JSON document.
    Once a restaurant-like record is found, do not recurse into its fields.
    """
    found = []

    if isinstance(obj, dict):
        has_name = obj.get("name") is not None
        has_identifier = any(
            obj.get(key) is not None
            for key in ("id", "restaurantSlug", "branchId", "branchName")
        )

        if has_name and has_identifier:
            record = dict(obj)
            record["_source_file"] = source_file
            found.append(record)
            return found

        for value in obj.values():
            found.extend(extract_records(value, source_file))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(extract_records(item, source_file))

    return found


def load_records():
    client = get_r2_client()
    bucket = os.environ["CF_R2_BUCKET_NAME"]

    records = []
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=R2_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]

            if not key.lower().endswith(".json"):
                continue

            response = client.get_object(Bucket=bucket, Key=key)
            content = response["Body"].read().decode("utf-8")

            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON: {key} ({exc})")
                continue

            records.extend(extract_records(data, key))

    # Keep every extracted source record. Do not group by ID or name.
    for index, record in enumerate(records):
        record["_record_index"] = index

    return records


# ============================================================
# PAIRWISE CANDIDATE DISCOVERY
# ============================================================

def get_pair_key(i, j):
    return (min(i, j), max(i, j))


def discover_candidates(records):
    """
    Find possible pairs without merging records, assigning groups,
    or changing the original records.
    """
    candidates = {}

    def add_candidate(i, j, reason, score=None):
        if i == j:
            return

        a = records[i]
        b = records[j]

        if not COMPARE_SAME_ID:
            id_a = a.get("id")
            id_b = b.get("id")
            if id_a is not None and id_b is not None and str(id_a) == str(id_b):
                return

        key = get_pair_key(i, j)

        if key not in candidates:
            candidates[key] = {
                "record_a_index": key[0],
                "record_b_index": key[1],
                "reasons": set(),
                "name_similarity": None,
                "reordered_similarity": None,
                "branchname_match": False,
            }

        candidate = candidates[key]
        candidate["reasons"].add(reason)

        if score is not None:
            if reason == "fuzzy":
                old = candidate["name_similarity"]
                candidate["name_similarity"] = (
                    score if old is None else max(old, score)
                )
            elif reason == "reordered":
                old = candidate["reordered_similarity"]
                candidate["reordered_similarity"] = (
                    score if old is None else max(old, score)
                )

    # --------------------------------------------------------
    # A. Compare unique names, then expand matching name pairs
    # --------------------------------------------------------

    name_to_indices = defaultdict(list)

    for i, record in enumerate(records):
        name = str(record.get("name") or "").strip()
        if name:
            name_to_indices[name].append(i)

    unique_names = list(name_to_indices.keys())

    print(f"Source records: {len(records)}")
    print(f"Unique raw names: {len(unique_names)}")
    print("Checking name pairs...")

    # Use RapidFuzz's fast C++ implementation for candidate discovery.
    # Compare every unique-name pair so there is no first-letter blocking.
    for name_a, name_b in combinations(unique_names, 2):
        reasons, scores = get_reasons(name_a, name_b)

        if not reasons:
            continue

        indices_a = name_to_indices[name_a]
        indices_b = name_to_indices[name_b]

        for i in indices_a:
            for j in indices_b:
                for reason in reasons:
                    score = None

                    if reason == "fuzzy":
                        score = scores["name_similarity"]
                    elif reason == "reordered":
                        score = scores["reordered_similarity"]

                    add_candidate(i, j, reason, score)

    # Identical raw names also need pairs between records with that name.
    for name, indices in name_to_indices.items():
        for i, j in combinations(indices, 2):
            add_candidate(i, j, "same_text")

    # --------------------------------------------------------
    # B. Branch-name candidates
    # A branchName matching another record's name is evidence
    # for manual review, not proof that they should be merged.
    # --------------------------------------------------------

    normalized_name_index = defaultdict(list)

    for i, record in enumerate(records):
        name_key = normalize_separators(record.get("name"))
        if name_key:
            normalized_name_index[name_key].append(i)

    for i, record in enumerate(records):
        branch_name = record.get("branchName")
        branch_key = normalize_separators(branch_name)

        if not branch_key:
            continue

        matching_indices = normalized_name_index.get(branch_key, [])

        for j in matching_indices:
            if i == j:
                continue

            add_candidate(i, j, "branchname")

    # --------------------------------------------------------
    # C. Convert candidates to output rows
    # --------------------------------------------------------

    rows = []

    for (i, j), candidate in candidates.items():
        a = records[i]
        b = records[j]

        row = {
            "record_a_index": i,
            "record_b_index": j,
            "match_reasons": " | ".join(sorted(candidate["reasons"])),
            "name_similarity": candidate["name_similarity"],
            "reordered_similarity": candidate["reordered_similarity"],
            "source_file_a": a.get("_source_file"),
            "source_file_b": b.get("_source_file"),
        }

        for field in OUTPUT_FIELDS:
            row[f"a_{field}"] = a.get(field)
            row[f"b_{field}"] = b.get(field)

        rows.append(row)

    rows.sort(
        key=lambda row: (
            row["a_name"] or "",
            row["b_name"] or "",
            row["record_a_index"],
            row["record_b_index"],
        )
    )

    return rows


# ============================================================
# EXPORT CSV + JSON
# ============================================================

def export_results(rows):
    df = pd.DataFrame(rows)

    if df.empty:
        # Create empty files with a predictable schema.
        columns = [
            "record_a_index",
            "record_b_index",
            "match_reasons",
            "name_similarity",
            "reordered_similarity",
            "source_file_a",
            "source_file_b",
        ]

        for field in OUTPUT_FIELDS:
            columns.extend([f"a_{field}", f"b_{field}"])

        df = pd.DataFrame(columns=columns)

    df.to_csv(CSV_FILE, index=False, encoding="utf-8-sig")

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(
            rows,
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    print(f"Candidate pairs: {len(rows)}")
    print(f"CSV saved: {CSV_FILE}")
    print(f"JSON saved: {JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

def main():
    records = load_records()

    print(f"Loaded source records: {len(records)}")

    rows = discover_candidates(records)
    export_results(rows)


if __name__ == "__main__":
    main()
