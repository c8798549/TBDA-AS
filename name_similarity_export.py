"""
name_similarity_export.py
==========================

Single purpose:
Collect restaurant names that may have any similarity into connected
components and export them to CSV + JSON for manual review.

Signals that connect two names:
  - same_text : Same text after removing spaces and punctuation.
  - separator : Same first segment after " - " or "("
                (comma is already the current grouping key).
  - fuzzy     : Spelling similarity >= SIM_FUZZY_MIN.
  - reordered : Similarity after sorting words >= SIM_REORDER_MIN.
  - prefix    : One name starts with another restaurant name (all levels).
  - branchname: A branchName starts with another restaurant name (all levels).

Nothing is applied to the data. This is a review-only export.

Uses the same environment variables as name_audit.py.
"""

import os
import re
import json
import difflib
import unicodedata
import csv
from collections import defaultdict, Counter

import boto3


R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

# ---------------- output files ----------------
CSV_FILE = "name_similarity_review.csv"
JSON_FILE = "name_similarity_review.json"

# ---------------- thresholds ----------------
SIM_FUZZY_MIN = 0.75       # Minimum spelling similarity
SIM_REORDER_MIN = 0.75     # Minimum similarity after sorting words
SIM_MAX_LEN_DIFF = 0.35    # Maximum relative length difference between names
SIM_BLOCK_PREFIX = 3       # Compare names within blocks sharing the first 3 characters

EST_MIN_NAMES = 2          # Minimum number of names for an established restaurant
EST_MIN_BRANCHES = 3       # Alternatively, minimum number of branches
MIN_HEAD_LEN = 4           # Minimum length of a base name for prefix matching

SEP_RE = re.compile(r"\s*(?:,|\(|\s[-–—]\s)\s*")
APOSTROPHES = re.compile(r"[\\'\`´‘’ʼ′ʻ]")


s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)

BUCKET = os.environ["CF_R2_BUCKET_NAME"]


# ============================================================
# HELPERS
# ============================================================

def load_records(key):
    body = (
        s3.get_object(Bucket=BUCKET, Key=key)["Body"]
        .read()
        .decode("utf-8")
    )
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
    value = str(value)

    if value.isdigit():
        return (0, int(value), value)

    return (1, 0, value)


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text)
    text = APOSTROPHES.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def first_segment(name):
    return name.split(",")[0].strip()


def brand_key(name):
    """Return the current grouping key: the segment before the first comma."""
    return normalize_text(first_segment(name))


def alt_first_part(name):
    """Return the first segment before a comma, hyphen separator, or opening parenthesis."""
    return SEP_RE.split(name, maxsplit=1)[0].strip()


def alt_brand_key(name):
    return normalize_text(alt_first_part(name))


def tokens(text):
    text = unicodedata.normalize("NFKC", text)
    text = APOSTROPHES.sub("", text)
    return re.findall(r"[^\W\_]+", text.lower())


def squash(text):
    return "".join(tokens(text))


def jaccard(a, b):
    if not a or not b:
        return 0.0

    return len(a & b) / len(a | b)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# MAIN
# ============================================================

def main():
    # ----------------------------------------------------
    # 1) Read input files
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

    names = set()
    id_to_names = defaultdict(Counter)
    name_to_ids = defaultdict(set)
    id_to_branch_ids = defaultdict(set)
    branch_city = defaultdict(set)
    id_to_cuisines = defaultdict(set)
    id_branch_brands = defaultdict(lambda: defaultdict(set))
    bk_example = {}
    restaurant_rows = []
    total_records = 0

    for i, key in enumerate(files, start=1):
        records = load_records(key)
        total_records += len(records)

        for r in records:
            _id = clean(r.get("id"))
            name = clean(r.get("name"))
            b_id = clean(r.get("branchId"))
            b_name = clean(r.get("branchName"))
            cuisine_str = clean(r.get("cuisineString"))

            city = r.get("shopCity")

            if isinstance(city, float) and city.is_integer():
                city = str(int(city))

            city = clean(city)

            if name:
                names.add(name)

            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)

            if _id and b_id:
                id_to_branch_ids[_id].add(b_id)

            if b_id and city:
                branch_city[b_id].add(city)

            if _id and cuisine_str:
                for part in cuisine_str.split(","):
                    p = re.sub(r"\s+", " ", part).strip().lower()
                    if p:
                        id_to_cuisines[_id].add(p)

            if _id and b_id and b_name:
                bk = brand_key(b_name)

                if bk:
                    id_branch_brands[_id][bk].add(b_id)
                    bk_example.setdefault(bk, b_name)

            restaurant_rows.append({
                "id": _id,
                "name": name,
                "restaurantSlug": clean(r.get("restaurantSlug")),
                "branchUrl": clean(r.get("branchUrl")),
            })

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    # ----------------------------------------------------
    # 2) Current grouping
    #    Group by the segment before the first comma and
    #    merge names associated with the same restaurant ID.
    # ----------------------------------------------------
    brand_to_names = defaultdict(set)
    brand_to_ids = defaultdict(set)
    brand_variants = defaultdict(Counter)

    for name in sorted(names):
        k = brand_key(name)

        if not k:
            continue

        brand_to_names[k].add(name)
        brand_variants[k][first_segment(name)] += 1
        brand_to_ids[k].update(name_to_ids[name])

    parent = {k: k for k in brand_to_names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]

        return x

    def union(a, b):
        ra, rb = find(a), find(b)

        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra

            parent[rb] = ra

    for rid, counter in id_to_names.items():
        ks = [brand_key(n) for n in counter]
        ks = [k for k in ks if k]

        for other in ks[1:]:
            union(ks[0], other)

    components = defaultdict(list)

    for k in brand_to_names:
        components[find(k)].append(k)

    groups = []
    key_to_group = {}

    for root in sorted(components):
        keys = sorted(components[root])

        g_names, g_ids = set(), set()
        variants = Counter()

        for k in keys:
            g_names |= brand_to_names[k]
            g_ids |= brand_to_ids[k]
            variants.update(brand_variants[k])

        top_variant = sorted(
            variants.items(),
            key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

        bids, cuisines, cities = set(), set(), set()
        branch_brands = defaultdict(set)

        for rid in g_ids:
            bids.update(id_to_branch_ids.get(rid, ()))
            cuisines.update(id_to_cuisines.get(rid, ()))

            for bk, bset in id_branch_brands.get(rid, {}).items():
                branch_brands[bk] |= bset

        for bid in bids:
            cities.update(branch_city.get(bid, ()))

        gi = len(groups)

        for k in keys:
            key_to_group[k] = gi

        groups.append({
            "display": top_variant,
            "main_key": brand_key(top_variant),
            "main_squash": squash(top_variant),
            "keys": keys,
            "names": g_names,
            "ids": g_ids,
            "branches": bids,
            "cuisines": cuisines,
            "cities": cities,
            "branch_brands": branch_brands,
        })

    total_groups = len(groups)
    print(f"Groups after current grouping: {total_groups}")

    def established(g):
        return (
            len(g["names"]) >= EST_MIN_NAMES
            or len(g["branches"]) >= EST_MIN_BRANCHES
        )

    # ----------------------------------------------------
    # 3) Build similarity edges
    # ----------------------------------------------------
    edges = set()

    def add_edge(a, b, reason):
        if a != b:
            edges.add((min(a, b), max(a, b), reason))

    # (a) Same text after removing spaces and punctuation
    squash_map = defaultdict(set)

    for gi, g in enumerate(groups):
        for k in g["keys"]:
            s = squash(k)

            if s:
                squash_map[s].add(gi)

    for s, gis in squash_map.items():
        gis = sorted(gis)

        for other in gis[1:]:
            add_edge(gis[0], other, "same_text")

    # (b) Same first segment after a hyphen separator or opening parenthesis
    alt_map = defaultdict(set)

    for gi, g in enumerate(groups):
        for n in g["names"]:
            ak = alt_brand_key(n)

            if ak:
                alt_map[ak].add(gi)

    for k, gis in alt_map.items():
        gis = sorted(gis)

        for other in gis[1:]:
            add_edge(gis[0], other, "separator")

    # (c) Spelling similarity and similarity after reordering words
    blocks = defaultdict(list)

    for gi, g in enumerate(groups):
        m = g["main_squash"]

        if m:
            blocks[m[:SIM_BLOCK_PREFIX]].append(gi)

    pairs_checked = 0

    for blk, members in blocks.items():
        for x in range(len(members)):
            a = groups[members[x]]

            for y in range(x + 1, len(members)):
                b = groups[members[y]]

                sa, sb = a["main_squash"], b["main_squash"]

                if sa == sb:
                    continue

                if abs(len(sa) - len(sb)) > SIM_MAX_LEN_DIFF * max(
                    len(sa), len(sb)
                ):
                    continue

                pairs_checked += 1

                sm = difflib.SequenceMatcher(None, sa, sb)

                if sm.real_quick_ratio() >= SIM_FUZZY_MIN:
                    r = sm.ratio()

                    if r >= SIM_FUZZY_MIN:
                        add_edge(
                            members[x],
                            members[y],
                            f"fuzzy:{r:.2f}"
                        )
                        continue

                ta = "".join(sorted(tokens(a["main_key"])))
                tb = "".join(sorted(tokens(b["main_key"])))

                if ta and tb and ta != tb:
                    r2 = difflib.SequenceMatcher(None, ta, tb).ratio()

                    if r2 >= SIM_REORDER_MIN:
                        add_edge(
                            members[x],
                            members[y],
                            f"reordered:{r2:.2f}"
                        )

    # (d) Prefix matching: a name starts with an established restaurant name
    head_index = defaultdict(list)

    for gi, g in enumerate(groups):
        if not established(g):
            continue

        for k in g["keys"]:
            t = tuple(tokens(k))

            if t and len("".join(t)) >= MIN_HEAD_LEN:
                if gi not in head_index[t]:
                    head_index[t].append(gi)

    for ki, g in enumerate(groups):
        best = None

        for k in g["keys"]:
            toks = tokens(k)

            for L in range(len(toks) - 1, 0, -1):
                heads = [
                    h for h in head_index.get(tuple(toks[:L]), ())
                    if h != ki
                ]

                if heads:
                    if best is None or L > best[0]:
                        best = (L, heads)
                    break

        if best is None:
            continue

        L, heads = best

        hi = max(
            heads,
            key=lambda h: (len(groups[h]["names"]), -h)
        )

        cj = round(jaccard(g["cuisines"], groups[hi]["cuisines"]), 2)

        add_edge(ki, hi, f"prefix:jaccard={cj}")

    # (e) A branchName starts with another restaurant name
    for gi, g in enumerate(groups):
        for bk, bset in g["branch_brands"].items():
            hi = key_to_group.get(bk)

            if hi is None or hi == gi:
                continue

            cj = round(jaccard(g["cuisines"], groups[hi]["cuisines"]), 2)

            add_edge(gi, hi, f"branchname:jaccard={cj}")

    # ----------------------------------------------------
    # 4) Connected components
    #    Multiple names can be connected through other names.
    # ----------------------------------------------------
    sim_parent = list(range(total_groups))

    def sim_find(x):
        while sim_parent[x] != x:
            sim_parent[x] = sim_parent[sim_parent[x]]
            x = sim_parent[x]

        return x

    for a, b, _r in edges:
        ra, rb = sim_find(a), sim_find(b)

        if ra != rb:
            sim_parent[max(ra, rb)] = min(ra, rb)

    comp_reasons = defaultdict(set)

    for a, b, reason in edges:
        comp_reasons[sim_find(a)].add(reason.split(":")[0])

    sim_components = defaultdict(list)

    for gi in range(total_groups):
        sim_components[sim_find(gi)].append(gi)

    # ----------------------------------------------------
    # 5) Export components containing multiple restaurants
    # ----------------------------------------------------
    rows = []
    flagged_groups = 0

    for root, member_indices in sim_components.items():
        if len(member_indices) < 2:
            continue

        flagged_groups += len(member_indices)

        members = sorted(
            member_indices,
            key=lambda gi: (
                -len(groups[gi]["ids"]),
                -len(groups[gi]["names"]),
                groups[gi]["display"].lower(),
            ),
        )

        label = groups[members[0]]["display"]
        reasons = sorted(comp_reasons.get(root, set()))
        other_displays = [
            groups[gi]["display"] for gi in members[1:]
        ]

        member_keys = set()

        for gi in members:
            member_keys.update(groups[gi]["keys"])

        for record in restaurant_rows:
            name = record.get("name")

            if name and brand_key(name) in member_keys:
                rows.append({
                    "Grouping label": label,
                    "id": record.get("id"),
                    "name": name,
                    "restaurantSlug": record.get("restaurantSlug"),
                    "branchUrl": record.get("branchUrl"),
                    "similarity_reason": "; ".join(reasons),
                    "other_restaurants_in_group": " | ".join(other_displays),
                })

    # Remove duplicate rows
    seen = set()
    deduped = []

    for row in rows:
        key = (
            row["Grouping label"],
            clean(row.get("id")) or "",
            clean(row.get("name")) or "",
            clean(row.get("branchUrl")) or "",
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(row)

    deduped.sort(
        key=lambda r: (
            str(r["Grouping label"] or "").lower(),
            str(r["name"] or "").lower(),
            str(r["id"] or ""),
        )
    )

    # ----------------------------------------------------
    # 6) Save outputs and report coverage
    # ----------------------------------------------------
    columns = [
        "Grouping label",
        "id",
        "name",
        "restaurantSlug",
        "branchUrl",
        "similarity_reason",
        "other_restaurants_in_group",
    ]

    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(deduped)

    save_json(JSON_FILE, {
        "note": (
            "All names connected by at least one similarity signal are "
            "placed in the same group. Exported for manual review only; "
            "no changes are applied to the data."
        ),
        "thresholds": {
            "fuzzy_min": SIM_FUZZY_MIN,
            "reorder_min": SIM_REORDER_MIN,
            "max_len_diff": SIM_MAX_LEN_DIFF,
            "block_prefix": SIM_BLOCK_PREFIX,
        },
        "edges": len(edges),
        "pairs_checked": pairs_checked,
        "groups_flagged": flagged_groups,
        "groups_clean": total_groups - flagged_groups,
        "records_exported": len(deduped),
        "records": deduped,
    })

    print(f"[done] edges={len(edges)} pairs_checked={pairs_checked}")
    print(
        f"[done] groups flagged={flagged_groups} / {total_groups} "
        f"(clean={total_groups - flagged_groups})"
    )
    print(f"[done] rows exported={len(deduped)}")
    print(f"-> {CSV_FILE}")
    print(f"-> {JSON_FILE}")


if __name__ == "__main__":
    main()
