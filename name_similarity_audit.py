import os
import re
import json
import csv
import unicodedata
from collections import defaultdict, Counter
from itertools import combinations

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

SUMMARY_FILE = "data_quality_summary.json"
UNIQUE_FILE = "unique_restaurants_by_id.json"
CANDIDATES_JSON = "name_similarity_candidates.json"
CANDIDATES_CSV = "name_similarity_candidates.csv"

# Candidate thresholds: intentionally inclusive because a human will validate.
FUZZY_MIN_RATIO = float(os.environ.get("FUZZY_MIN_RATIO", "0.86"))
TOKEN_JACCARD_MIN = float(os.environ.get("TOKEN_JACCARD_MIN", "0.60"))
MAX_FUZZY_BLOCK = int(os.environ.get("MAX_FUZZY_BLOCK", "500"))

FIELDS = [
    "id", "name", "restaurantSlug", "branchUrl", "branchName",
    "grouping_reason", "shopCity", "logo"
]
SOURCE_FIELDS = ["id", "name", "restaurantSlug", "branchUrl",
                 "branchName", "shopCity", "logo"]

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]

APOSTROPHES = re.compile(r"[\'`´‘’ʼ′ʻ]")
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
SEPARATOR_RE = re.compile(r"\s*(?:,|\(|\)|\s[-–—]\s)\s*")


def clean(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = str(value).strip()
    return value or None


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = APOSTROPHES.sub("", value)
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value):
    return TOKEN_RE.findall(normalize_text(value))


def squash(value):
    return "".join(tokens(value))


def first_segment(value):
    # Original name's first comma-separated part, e.g. "Diet Care , Dasma".
    return re.split(r",", str(value or ""), maxsplit=1)[0].strip()


def separator_base(value):
    # Also handles parentheses and spaced hyphens, e.g. "Starbucks (Ashbeliah)".
    return SEPARATOR_RE.split(str(value or ""), maxsplit=1)[0].strip()


def token_jaccard(a, b):
    aa, bb = set(tokens(a)), set(tokens(b))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def unique_values(values):
    result = []
    seen = set()
    for value in values:
        value = clean(value)
        if value is not None and value not in seen:
            seen.add(value)
            result.append(value)
    return sorted(result, key=lambda x: x.casefold())


def display_values(values):
    vals = unique_values(values)
    return " || ".join(vals) if vals else None


def load_records(key):
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    data = json.loads(body.decode("utf-8-sig"))
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("data", "records", "items", "results"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
        return [data]
    return []


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    # 1) List every JSON file under the requested R2 prefix.
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".json"):
                files.append((key, obj.get("Size", 0)))
    files.sort(key=lambda x: x[0])

    print(f"R2 prefix: {R2_PREFIX}")
    print(f"JSON files found: {len(files)}")

    per_file = []
    raw_record_count = 0
    missing_id_count = 0
    id_to_rows = defaultdict(list)
    id_first_seen = {}
    id_to_files = defaultdict(set)
    id_name_counts = defaultdict(Counter)

    # 2) Read all files and retain every record under its cleaned ID.
    for file_index, (key, size) in enumerate(files, start=1):
        records = load_records(key)
        raw_record_count += len(records)
        file_missing_ids = 0

        for row in records:
            rid = clean(row.get("id"))
            if not rid:
                missing_id_count += 1
                file_missing_ids += 1
                continue

            # Store only source fields needed for the deduplicated/audit exports.
            selected = {field: clean(row.get(field)) for field in SOURCE_FIELDS}
            id_to_rows[rid].append(selected)
            id_first_seen.setdefault(rid, key)
            id_to_files[rid].add(key)
            if selected.get("name"):
                id_name_counts[rid][selected["name"]] += 1

        per_file.append({
            "file": key,
            "size_bytes": size,
            "records": len(records),
            "records_missing_id": file_missing_ids,
        })
        print(f"[{file_index}/{len(files)}] {key}: {len(records)} records")

    unique_id_count = len(id_to_rows)
    records_with_id = raw_record_count - missing_id_count
    duplicate_records_by_id = records_with_id - unique_id_count

    # One output record per unique id. Preserve all observed values for fields
    # that may differ between source rows, instead of silently dropping variants.
    unique_records = []
    profiles = []
    for rid in sorted(id_to_rows, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x.casefold())):
        rows = id_to_rows[rid]
        profile = {"id": rid}
        for field in SOURCE_FIELDS:
            if field == "id":
                continue
            profile[field] = display_values(row.get(field) for row in rows)

        unique_records.append({
            **profile,
            "source_record_count": len(rows),
            "source_files": sorted(id_to_files[rid]),
        })

        names = unique_values(row.get("name") for row in rows)
        branch_names = unique_values(row.get("branchName") for row in rows)
        slugs = unique_values(row.get("restaurantSlug") for row in rows)
        urls = unique_values(row.get("branchUrl") for row in rows)
        cities = unique_values(row.get("shopCity") for row in rows)
        logos = unique_values(row.get("logo") for row in rows)

        # Each unique ID remains its own review entity; no IDs are grouped/merged.
        profiles.append({
            "id": rid,
            "name": display_values(names),
            "restaurantSlug": display_values(slugs),
            "branchUrl": display_values(urls),
            "branchName": display_values(branch_names),
            "shopCity": display_values(cities),
            "logo": display_values(logos),
            "_names": names,
            "_branch_names": branch_names,
            "_norm_names": sorted({normalize_text(n) for n in names if normalize_text(n)}),
            "_squashed_names": sorted({squash(n) for n in names if squash(n)}),
            "_comma_bases": sorted({normalize_text(first_segment(n)) for n in names if normalize_text(first_segment(n))}),
            "_separator_bases": sorted({normalize_text(separator_base(n)) for n in names if normalize_text(separator_base(n))}),
            "_tokens": sorted({tuple(tokens(n)) for n in names if tokens(n)}),
        })

    # 3) Candidate generation. Rules only create review candidates; they do not
    # change the unique-ID data or merge records.
    pair_reasons = defaultdict(set)

    def add_pairs_for_key(mapping, reason):
        for _key, indices in mapping.items():
            ids = sorted(set(indices))
            if len(ids) > 1:
                for a, b in combinations(ids, 2):
                    if a != b:
                        pair_reasons[(a, b)].add(reason)

    exact_map = defaultdict(list)
    squash_map = defaultdict(list)
    comma_base_map = defaultdict(list)
    separator_base_map = defaultdict(list)
    first_token_map = defaultdict(list)
    token_prefix_map = defaultdict(list)
    prefix_char_map = defaultdict(list)
    branch_name_map = defaultdict(list)

    for i, p in enumerate(profiles):
        for v in p["_norm_names"]:
            exact_map[v].append(i)
        for v in p["_squashed_names"]:
            squash_map[v].append(i)
        for v in p["_comma_bases"]:
            comma_base_map[v].append(i)
        for v in p["_separator_bases"]:
            separator_base_map[v].append(i)

        for name in p["_names"]:
            ts = tokens(name)
            if ts:
                first_token_map[ts[0]].append(i)
                # Prefix candidates are reviewed, not assumed to be the same brand.
                for end in range(1, min(len(ts), 5) + 1):
                    token_prefix_map[" ".join(ts[:end])].append(i)
            sq = squash(name)
            if len(sq) >= 3:
                prefix_char_map[sq[:3]].append(i)

        for branch in p["_branch_names"]:
            bn = normalize_text(branch)
            if bn:
                branch_name_map[bn].append(i)
                # A branchName may itself carry the base restaurant name.
                for n in p["_norm_names"]:
                    if n and (bn == n or bn.startswith(n + " ") or bn.startswith(n + ",")):
                        branch_name_map["branch-evidence:" + n].append(i)

    add_pairs_for_key(exact_map, "exact_name_after_case_punctuation_whitespace_normalization")
    add_pairs_for_key(squash_map, "same_name_after_removing_spaces_and_punctuation")
    add_pairs_for_key(comma_base_map, "same_first_segment_before_comma")
    add_pairs_for_key(separator_base_map, "same_base_before_comma_parenthesis_or_spaced_dash")
    add_pairs_for_key(token_prefix_map, "shared_name_prefix_up_to_5_tokens")
    add_pairs_for_key(branch_name_map, "branchName_name_evidence")

    # Fuzzy spelling + token-overlap comparisons are bounded to blocks.
    # Very large blocks are skipped for pairwise fuzzy comparison but exact,
    # base-name, and prefix rules still contribute candidates.
    fuzzy_comparisons = 0
    skipped_fuzzy_blocks = 0
    for block, indices in prefix_char_map.items():
        members = sorted(set(indices))
        if len(members) < 2:
            continue
        if len(members) > MAX_FUZZY_BLOCK:
            skipped_fuzzy_blocks += 1
            continue
        for a, b in combinations(members, 2):
            fuzzy_comparisons += 1
            pa, pb = profiles[a], profiles[b]
            best_ratio = 0.0
            best_pair = None
            for na in pa["_squashed_names"]:
                for nb in pb["_squashed_names"]:
                    if not na or not nb:
                        continue
                    # Lightweight similarity ratio without external dependencies.
                    from difflib import SequenceMatcher
                    ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio()
                    if ratio > best_ratio:
                        best_ratio, best_pair = ratio, (na, nb)
            if best_ratio >= FUZZY_MIN_RATIO:
                pair_reasons[(a, b)].add(f"similar_spelling_ratio_at_least_{FUZZY_MIN_RATIO:.2f}")

            # Catch names with the same brand tokens but branch/location additions.
            best_jaccard = 0.0
            for na in pa["_names"]:
                for nb in pb["_names"]:
                    best_jaccard = max(best_jaccard, token_jaccard(na, nb))
            if best_jaccard >= TOKEN_JACCARD_MIN:
                pair_reasons[(a, b)].add(f"shared_name_tokens_jaccard_at_least_{TOKEN_JACCARD_MIN:.2f}")

    # Include branch-name based evidence across IDs: if a branchName contains
    # another ID's restaurant name, make a candidate pair.
    normalized_name_index = defaultdict(set)
    for i, p in enumerate(profiles):
        for name in p["_norm_names"]:
            if name:
                normalized_name_index[name].add(i)

    for i, p in enumerate(profiles):
        for branch in p["_branch_names"]:
            bn = normalize_text(branch)
            if not bn:
                continue
            for name, other_ids in normalized_name_index.items():
                if len(name) >= 3 and (bn == name or bn.startswith(name + " ") or (" " + name + " ") in (" " + bn + " ")):
                    for j in other_ids:
                        if i != j:
                            a, b = sorted((i, j))
                            pair_reasons[(a, b)].add("branchName_contains_other_restaurant_name")

    # Build one candidate row per unique ID participating in any candidate pair.
    # Candidate group is a connected component for navigation only, NOT a merge.
    adjacency = defaultdict(set)
    for (a, b), reasons in pair_reasons.items():
        adjacency[a].add(b)
        adjacency[b].add(a)

    candidate_indices = set(adjacency)
    seen = set()
    components = []
    for start in sorted(candidate_indices):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        component = []
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(sorted(component))

    component_id = {}
    for group_num, component in enumerate(
        sorted(components, key=lambda c: (-len(c), min(profiles[i]["id"] for i in c))),
        start=1
    ):
        label = f"CANDIDATE_{group_num:06d}"
        for i in component:
            component_id[i] = label

    reasons_by_index = defaultdict(set)
    matched_indices = defaultdict(set)
    for (a, b), why in pair_reasons.items():
        reasons_by_index[a].update(why)
        reasons_by_index[b].update(why)
        matched_indices[a].add(b)
        matched_indices[b].add(a)

    candidate_rows = []
    for i in sorted(candidate_indices, key=lambda x: (component_id.get(x, ""), profiles[x]["id"])):
        p = profiles[i]
        reasons = reasons_by_index[i]
        matched_ids = {profiles[j]["id"] for j in matched_indices[i]}

        candidate_rows.append({
            "id": p["id"],
            "name": p["name"],
            "restaurantSlug": p["restaurantSlug"],
            "branchUrl": p["branchUrl"],
            "branchName": p["branchName"],
            "grouping_reason": " | ".join(sorted(reasons)),
            "shopCity": p["shopCity"],
            "logo": p["logo"],
            "candidate_group_id": component_id.get(i),
            "matched_unique_ids": " || ".join(sorted(matched_ids)),
            "candidate_pair_count": len(adjacency[i]),
        })

    pair_count_by_reason = Counter()
    for reasons in pair_reasons.values():
        for reason in reasons:
            pair_count_by_reason[reason] += 1

    summary = {
        "r2_prefix": R2_PREFIX,
        "json_files_found": len(files),
        "total_records_across_all_files": raw_record_count,
        "records_missing_id": missing_id_count,
        "records_with_id": records_with_id,
        "unique_non_empty_ids": unique_id_count,
        "duplicate_records_beyond_one_per_id": duplicate_records_by_id,
        "unique_ids_with_multiple_source_rows": sum(1 for rows in id_to_rows.values() if len(rows) > 1),
        "unique_ids_with_multiple_names": sum(1 for rid, counts in id_name_counts.items() if len(counts) > 1),
        "candidate_unique_ids": len(candidate_indices),
        "candidate_pairs": len(pair_reasons),
        "candidate_components_for_manual_navigation_only": len(components),
        "fuzzy_comparisons_performed": fuzzy_comparisons,
        "fuzzy_blocks_skipped_as_too_large": skipped_fuzzy_blocks,
        "candidate_pairs_by_reason": dict(sorted(pair_count_by_reason.items())),
        "records_per_file": per_file,
        "note": "No restaurant records or IDs were merged. Candidate groups are only review labels for manual validation."
    }

    save_json(SUMMARY_FILE, summary)
    save_json(UNIQUE_FILE, unique_records)
    save_json(CANDIDATES_JSON, candidate_rows)

    with open(CANDIDATES_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS + ["candidate_group_id", "matched_unique_ids", "candidate_pair_count"]
        )
        writer.writeheader()
        writer.writerows(candidate_rows)

    print("\n========== SUMMARY ==========")
    print(f"JSON files: {len(files)}")
    print(f"Total source records: {raw_record_count}")
    print(f"Records missing id: {missing_id_count}")
    print(f"Unique non-empty IDs: {unique_id_count}")
    print(f"Duplicate records beyond one per ID: {duplicate_records_by_id}")
    print(f"Unique IDs with multiple names: {summary['unique_ids_with_multiple_names']}")
    print(f"Candidate IDs for manual validation: {len(candidate_indices)}")
    print(f"Candidate pairs: {len(pair_reasons)}")
    print(f"Candidate components (review labels only): {len(components)}")
    print(f"Saved: {SUMMARY_FILE}, {UNIQUE_FILE}, {CANDIDATES_JSON}, {CANDIDATES_CSV}")


if __name__ == "__main__":
    main()
