import os
import re
import json
import csv
import unicodedata
from collections import defaultdict
from itertools import combinations
from typing import Any, Dict, List, Set, Tuple

import boto3
from rapidfuzz import fuzz


# ============================================================
# CONFIG
# ============================================================

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/",
)

CSV_FILE = "restaurant_groups.csv"
JSON_FILE = "restaurant_groups.json"

# Fuzzy threshold (kept high to reduce noise)
SIM_FUZZY_MIN = 0.85
# Prefix minimum length on the squashed name
MIN_PREFIX_LENGTH = 4
# Max length difference ratio before attempting fuzzy
SIM_MAX_LEN_DIFF = 0.35
# Skip buckets larger than this (they explode O(n^2))
MAX_BUCKET_SIZE = 300

# Output columns (in order)
OUTPUT_COLUMNS = [
    "id",
    "name",
    "restaurantSlug",
    "branchUrl",
    "branchName",
    "group_id",
    "grouping_reason",
    "is_primary",
    "matched_with_ids",
    "source_file",
    "record_index",
]

APOSTROPHES = re.compile(r"[\\'`´‘’ʼ′ʻ]")
SEP_RE = re.compile(r"\s*(?:,|\(|\s[-–—]\s)\s*")


# ============================================================
# R2 CLIENT
# ============================================================

required_env = [
    "CF_R2_ENDPOINT_URL",
    "CF_R2_ACCESS_KEY_ID",
    "CF_R2_SECRET_ACCESS_KEY",
    "CF_R2_BUCKET_NAME",
]

missing_env = [k for k in required_env if not os.environ.get(k)]
if missing_env:
    raise RuntimeError("Missing environment variables: " + ", ".join(missing_env))

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
    region_name="auto",
)

BUCKET = os.environ["CF_R2_BUCKET_NAME"]


# ============================================================
# NORMALIZATION HELPERS
# ============================================================

def clean(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    value = APOSTROPHES.sub("", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def squash(value: Any) -> str:
    """Keep only letters/digits."""
    value = normalize_text(value)
    return "".join(ch for ch in value if ch.isalnum())


def tokens(value: Any) -> List[str]:
    value = normalize_text(value)
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def first_segment(value: Any) -> str:
    value = str(value or "").strip()
    return SEP_RE.split(value, maxsplit=1)[0].strip()


# ============================================================
# LOAD RECORDS
# ============================================================

def load_records(key: str) -> List[Dict[str, Any]]:
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    data = json.loads(body)

    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]

    if isinstance(data, dict):
        for k in ("data", "records", "items", "results"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
        return [data]

    return []


def read_all_records() -> List[Dict[str, Any]]:
    paginator = s3.get_paginator("list_objects_v2")
    keys: List[str] = []

    for page in paginator.paginate(Bucket=BUCKET, Prefix=R2_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                keys.append(obj["Key"])

    keys.sort()
    print(f"📁 JSON files found: {len(keys)}")

    records: List[Dict[str, Any]] = []

    for n, key in enumerate(keys, 1):
        try:
            file_records = load_records(key)
        except Exception as exc:
            print(f"⚠️ Skipped {key}: {exc}")
            continue

        print(f"[{n}/{len(keys)}] {key} -> {len(file_records)} records")

        for rec in file_records:
            records.append({
                "record_index": len(records),
                "source_file": key,
                "data": rec,
            })

    print(f"🧮 Total records loaded: {len(records)}")
    return records


# ============================================================
# PAIR ACCUMULATOR
# ============================================================

class PairStore:
    """Collect pairs with all their match reasons + scores."""

    def __init__(self):
        self.store: Dict[Tuple[int, int], Dict[str, Any]] = {}

    def add(self, i: int, j: int, reason: str, score: float | None = None):
        if i == j:
            return
        pair = (min(i, j), max(i, j))
        entry = self.store.setdefault(
            pair,
            {"reasons": set(), "fuzzy_score": None},
        )
        entry["reasons"].add(reason)
        if score is not None:
            if entry["fuzzy_score"] is None or score > entry["fuzzy_score"]:
                entry["fuzzy_score"] = score

    def __len__(self):
        return len(self.store)


# ============================================================
# BLOCKING KEYS
# ============================================================

def blocking_keys(record: Dict[str, Any]) -> Dict[str, str]:
    keys: Dict[str, str] = {}

    name = clean(record.get("name"))
    if name:
        toks = tokens(name)
        sq = squash(name)

        if toks:
            keys["first_token"] = toks[0]
        if sq:
            keys["squash4"] = sq[:4]
        if len(toks) > 1:
            keys["sorted_tokens"] = " ".join(sorted(toks))

    branch = clean(record.get("branchName"))
    if branch:
        keys["branch_first_token"] = " ".join(tokens(branch)[:1])

    rid = record.get("id")
    if rid not in (None, ""):
        keys["id"] = str(rid)

    return keys


def build_buckets(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[int]]]:
    buckets: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))

    for idx, item in enumerate(records):
        for btype, key in blocking_keys(item["data"]).items():
            buckets[btype][key].append(idx)

    return buckets


# ============================================================
# NAME MATCHING
# ============================================================

def name_reasons(name_a: str, name_b: str) -> Tuple[Set[str], float | None]:
    reasons: Set[str] = set()

    if name_a == name_b:
        return {"same_text"}, None

    a_norm = normalize_text(name_a)
    b_norm = normalize_text(name_b)

    if a_norm == b_norm:
        reasons.add("case_insensitive")

    a_sq = squash(name_a)
    b_sq = squash(name_b)

    if a_sq and a_sq == b_sq:
        reasons.add("separator")

    a_tok = tokens(name_a)
    b_tok = tokens(name_b)

    if len(a_tok) > 1 and len(b_tok) > 1:
        if sorted(a_tok) == sorted(b_tok) and a_tok != b_tok:
            reasons.add("reordered")

    if a_sq and b_sq and a_sq != b_sq:
        shorter, longer = (a_sq, b_sq) if len(a_sq) <= len(b_sq) else (b_sq, a_sq)
        if len(shorter) >= MIN_PREFIX_LENGTH and longer.startswith(shorter):
            reasons.add("prefix")

    fuzzy_score = None
    if not reasons and a_sq and b_sq:
        max_len = max(len(a_sq), len(b_sq))
        if max_len and abs(len(a_sq) - len(b_sq)) / max_len <= SIM_MAX_LEN_DIFF:
            score = fuzz.ratio(a_sq, b_sq) / 100.0
            if score >= SIM_FUZZY_MIN:
                reasons.add("fuzzy")
                fuzzy_score = round(score, 4)

    return reasons, fuzzy_score


def process_buckets(
    records: List[Dict[str, Any]],
    buckets: Dict[str, Dict[str, List[int]]],
    pairs: PairStore,
):
    total_buckets = sum(len(v) for v in buckets.values())
    done = 0
    skipped = 0

    for btype, key_map in buckets.items():
        for key, indices in key_map.items():
            done += 1
            if done % 500 == 0:
                print(f"  ... buckets {done}/{total_buckets}, pairs so far {len(pairs)}")

            if len(indices) < 2:
                continue

            if len(indices) > MAX_BUCKET_SIZE:
                skipped += 1
                print(f"  ⏭ Skipped huge bucket [{btype}:{key}] size={len(indices)}")
                continue

            for i, j in combinations(indices, 2):
                rec_a = records[i]["data"]
                rec_b = records[j]["data"]

                # same id, different name
                id_a = rec_a.get("id")
                id_b = rec_b.get("id")
                if id_a not in (None, "") and str(id_a) == str(id_b):
                    name_a = clean(rec_a.get("name")) or ""
                    name_b = clean(rec_b.get("name")) or ""
                    if squash(name_a) != squash(name_b):
                        pairs.add(i, j, "same_id_diff_name")
                    else:
                        pairs.add(i, j, "same_id")

                name_a = clean(rec_a.get("name"))
                name_b = clean(rec_b.get("name"))

                if name_a and name_b:
                    reasons, fuzzy_score = name_reasons(name_a, name_b)
                    for r in reasons:
                        pairs.add(i, j, r, fuzzy_score)

                branch_a = squash(rec_a.get("branchName") or "")
                branch_b = squash(rec_b.get("branchName") or "")
                name_a_sq = squash(name_a or "")
                name_b_sq = squash(name_b or "")

                if branch_a and branch_a == name_b_sq:
                    pairs.add(i, j, "branchname")
                if branch_b and branch_b == name_a_sq:
                    pairs.add(i, j, "branchname")

    print(f"⏭ Skipped {skipped} oversized buckets")


# ============================================================
# UNION-FIND
# ============================================================

class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ============================================================
# GROUP BUILDING
# ============================================================

# Order of priority for choosing the "primary reason" of the whole group
REASON_PRIORITY = [
    "same_id",
    "same_id_diff_name",
    "same_text",
    "case_insensitive",
    "separator",
    "reordered",
    "branchname",
    "prefix",
    "fuzzy",
]


def pick_group_reason(all_reasons: Set[str]) -> str:
    for r in REASON_PRIORITY:
        if r in all_reasons:
            return r
    return "unknown"


def build_groups(
    records: List[Dict[str, Any]],
    pairs: PairStore,
) -> List[Dict[str, Any]]:
    uf = UnionFind(len(records))

    # Track reasons per group root
    group_reasons: Dict[int, Set[str]] = defaultdict(set)
    # For is_primary, track which record is the "primary" of each group
    # Rule: the one with the shortest original name; ties -> lower record_index
    group_members: Dict[int, List[int]] = defaultdict(list)

    for (i, j), info in pairs.store.items():
        uf.union(i, j)

    # Collect members + reasons per group
    for (i, j), info in pairs.store.items():
        root = uf.find(i)
        group_reasons[root].update(info["reasons"])

    for idx in range(len(records)):
        root = uf.find(idx)
        group_members[root].append(idx)

    # ---- Build the output rows ----
    rows: List[Dict[str, Any]] = []
    sorted_roots = sorted(group_members.keys(), key=lambda r: min(group_members[r]))

    group_id_counter = 0
    for root in sorted_roots:
        members = group_members[root]
        # Skip singletons
        if len(members) < 2:
            continue

        group_id_counter += 1
        gid = group_id_counter

        reasons = group_reasons.get(root, set())
        group_reason = pick_group_reason(reasons)

        # Pick primary: shortest name, then lowest id, then lowest record_index
        def primary_sort_key(idx: int):
            rec = records[idx]["data"]
            name = clean(rec.get("name")) or ""
            rid = rec.get("id")
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                rid_int = 10**18
            return (len(name), rid_int, idx)

        primary_idx = min(members, key=primary_sort_key)

        member_ids = [
            records[m]["data"].get("id")
            for m in members
            if records[m]["data"].get("id") not in (None, "")
        ]
        member_ids_str = ", ".join(str(x) for x in member_ids)

        for m in members:
            rec = records[m]["data"]
            rows.append({
                "id": rec.get("id"),
                "name": rec.get("name"),
                "restaurantSlug": rec.get("restaurantSlug"),
                "branchUrl": rec.get("branchUrl"),
                "branchName": rec.get("branchName"),
                "group_id": gid,
                "grouping_reason": group_reason,
                "is_primary": "true" if m == primary_idx else "false",
                "matched_with_ids": member_ids_str,
                "source_file": records[m]["source_file"],
                "record_index": m,
            })

    # Sort output: by group_id, then primary first, then name
    rows.sort(key=lambda r: (
        r["group_id"],
        0 if r["is_primary"] == "true" else 1,
        str(r["name"] or "").casefold(),
    ))

    return rows


# ============================================================
# EXPORT
# ============================================================

def export_results(rows: List[Dict[str, Any]]):
    # JSON
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)

    # CSV
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    unique_groups = len({r["group_id"] for r in rows})
    print(f"\n✅ Total grouped rows : {len(rows)}")
    print(f"✅ Unique groups      : {unique_groups}")
    print(f"💾 CSV saved: {CSV_FILE}")
    print(f"💾 JSON saved: {JSON_FILE}")


# ============================================================
# MAIN
# ============================================================

def main():
    records = read_all_records()

    print("\n🔨 Building blocking buckets...")
    buckets = build_buckets(records)

    for btype, key_map in buckets.items():
        sizes = sorted((len(v) for v in key_map.values()), reverse=True)
        print(f"  [{btype}] buckets={len(key_map)}, max_bucket={sizes[0] if sizes else 0}")

    print("\n🔍 Matching within buckets...")
    pairs = PairStore()
    process_buckets(records, buckets, pairs)
    print(f"🔗 Total candidate pairs: {len(pairs)}")

    print("\n🧩 Building groups (union-find)...")
    rows = build_groups(records, pairs)

    print("\n📤 Exporting results...")
    export_results(rows)


if __name__ == "__main__":
    main()