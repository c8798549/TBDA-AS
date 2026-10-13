# -*- coding: utf-8 -*-
"""
find_similar_names.py
---------------------
1) Reads all JSON files from R2 under a given prefix
2) Prints the record count of every file + the total
3) Combines all records and deduplicates by unique id
4) Writes a CSV with every id whose name (or other data) is similar
   to another id -- WITHOUT any grouping. These are only candidates
   for manual validation.

Output columns:
    cluster_label, id, name, restaurantSlug, branchUrl, branchName,
    grouping_reason, shopCity, logo

Run:
    export CF_R2_ENDPOINT_URL=...
    export CF_R2_ACCESS_KEY_ID=...
    export CF_R2_SECRET_ACCESS_KEY=...
    export CF_R2_BUCKET_NAME=...
    python find_similar_names.py
"""

import os
import re
import csv
import json
import difflib
import unicodedata
from collections import defaultdict, Counter

import boto3

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
R2_PREFIX = os.environ.get(
    "R2_PREFIX", "merged-restaurant-info/year=2025/month=09/day=17/"
)
OUT_FILE = os.environ.get("OUT_FILE", "similar_name_candidates.csv")

# ---------------- thresholds (tune from here) -------------------
FUZZY_MIN_RATIO = 0.85    # minimum spelling similarity (squash vs squash)
MAX_BLOCK_SIZE = 400      # max block size compared inside fuzzy matching
MIN_FIRST_WORD = 3        # minimum length of the first word to be a method
MIN_PREFIX_LEN = 4        # minimum length (chars) of a prefix to be a method

# words/tokens that appear at the end of names but are not part of the brand
NOISE_WORDS = {
    "mall", "branch", "branches", "co-op", "coop", "co", "op",
    "city", "center", "centre", "tower", "complex", "plaza",
    "block", "avenue", "ave", "road", "st", "street", "area",
    "kuwait", "kw", "q8",
}

APOSTROPHES = re.compile(r"['`´‘’ʼ′ʻ]")
SEP_RE = re.compile(r"\s*(?:,|\(|\)|\s[-–—]\s)\s*")

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------
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


def norm_code(value):
    """Normalize city codes: 1 and 1.0 and '1' all become '1'."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text or None


def normalize_text(text):
    text = unicodedata.normalize("NFKC", str(text))
    text = APOSTROPHES.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def tokens(text):
    text = unicodedata.normalize("NFKC", str(text))
    text = APOSTROPHES.sub("", text)
    return re.findall(r"[^\W_]+", text.lower())


def squash(text):
    """Text without spaces or punctuation."""
    return "".join(tokens(text))


def first_part(name):
    """First part before a comma / parenthesis / dash."""
    return SEP_RE.split(str(name), maxsplit=1)[0].strip()


def brand_key(name):
    return normalize_text(first_part(name))


def strip_noise_key(name):
    """Brand key after removing trailing noise words / digits."""
    t = tokens(first_part(name))
    while len(t) > 1 and (t[-1] in NOISE_WORDS or t[-1].isdigit()):
        t.pop()
    return " ".join(t)


def url_slug(url):
    """Last path segment of a URL (normalized)."""
    if not url:
        return None
    m = re.search(r"/([^/?#]+)/?$", str(url))
    if not m:
        return None
    s = normalize_text(m.group(1))
    return s or None


# ---------------------------------------------------------------
# STEP 1+2+3: READ / COUNT / DEDUP BY ID
# ---------------------------------------------------------------
def main():
    # ---- list files ----
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                files.append(obj["Key"])
    files.sort()
    print(f"Files found: {len(files)}")

    # ---- read + aggregate per unique id ----
    # id -> {field: Counter of values}
    ids = defaultdict(lambda: defaultdict(Counter))

    total_records = 0
    for i, key in enumerate(files, start=1):
        records = load_records(key)
        total_records += len(records)

        for r in records:
            _id = clean(r.get("id"))
            if not _id:
                continue
            bag = ids[_id]
            for field in ("name", "restaurantSlug", "branchUrl",
                          "branchName", "shopCity", "logo"):
                v = r.get(field)
                if field == "shopCity":
                    v = norm_code(v)
                else:
                    v = clean(v)
                if v:
                    bag[field][v] += 1

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    print("\n================ COUNTS ================")
    print(f"Total records (all files) : {total_records}")
    print(f"Unique ids after dedup    : {len(ids)}")

    # ---- canonical row per id ----
    def canon(counter):
        if not counter:
            return None
        # most frequent value; tie-break on shortest value
        return sorted(counter.items(), key=lambda x: (-x[1], len(x[0])))[0][0]

    rec = {}
    for _id, bag in ids.items():
        name = canon(bag["name"])
        if not name:
            continue   # cannot compare without a name
        rec[_id] = {
            "id": _id,
            "name": name,
            "restaurantSlug": canon(bag["restaurantSlug"]),
            "branchUrl": canon(bag["branchUrl"]),
            "branchName": canon(bag["branchName"]),
            "shopCity": canon(bag["shopCity"]),
            "logo": canon(bag["logo"]),
        }

    print(f"Unique ids with a name    : {len(rec)}")

    # ---------------------------------------------------------------
    # STEP 4: all similarity methods -- each one puts ids on one key
    # ---------------------------------------------------------------
    reasons = defaultdict(set)          # id -> set("method[key]")

    def add(method, key, id_list):
        """If the key is shared by 2+ ids, record the reason for each."""
        uniq = sorted(set(id_list))
        if len(uniq) < 2:
            return
        for _id in uniq:
            reasons[_id].add(f"{method}[{key}]")

    # indexes: normalized key -> list of ids
    idx_exact = defaultdict(list)       # normalized full name
    idx_squash = defaultdict(list)      # no spaces / punctuation
    idx_brand = defaultdict(list)       # part before the comma
    idx_altbrand = defaultdict(list)    # part before any separator
    idx_noise = defaultdict(list)       # after removing noise words
    idx_first = defaultdict(list)       # first word
    idx_slug = defaultdict(list)        # restaurantSlug
    idx_urlslug = defaultdict(list)     # slug from branchUrl
    idx_logo = defaultdict(list)        # logo
    idx_bnbrand = defaultdict(list)     # brand key from branchName

    for _id, r in rec.items():
        n = r["name"]
        idx_exact[normalize_text(n)].append(_id)
        idx_squash[squash(n)].append(_id)
        bk = brand_key(n)
        ak = normalize_text(first_part(n))
        if bk:
            idx_brand[bk].append(_id)
        if ak:
            idx_altbrand[ak].append(_id)
        nk = strip_noise_key(n)
        if nk:
            idx_noise[nk].append(_id)
        t = tokens(n)
        if t and len(t[0]) >= MIN_FIRST_WORD:
            idx_first[t[0]].append(_id)
        if r["restaurantSlug"]:
            idx_slug[normalize_text(r["restaurantSlug"])].append(_id)
        us = url_slug(r["branchUrl"])
        if us:
            idx_urlslug[us].append(_id)
        if r["logo"]:
            idx_logo[normalize_text(r["logo"])].append(_id)
        if r["branchName"]:
            bb = brand_key(r["branchName"])
            if bb:
                idx_bnbrand[bb].append(_id)

    # methods 1..7: direct name-based
    for key, v in idx_exact.items():
        add("same_name", key, v)
    for key, v in idx_squash.items():
        add("same_text_no_punct", key, v)
    for key, v in idx_brand.items():
        add("same_brand_before_comma", key, v)
    for key, v in idx_altbrand.items():
        add("same_brand_before_any_separator", key, v)
    for key, v in idx_noise.items():
        add("same_brand_minus_noise_words", key, v)
    for key, v in idx_first.items():
        add("same_first_word", key, v)

    # methods 8..10: data-based (not the name itself)
    for key, v in idx_slug.items():
        add("same_restaurant_slug", key, v)
    for key, v in idx_urlslug.items():
        add("same_branch_url_slug", key, v)
    for key, v in idx_logo.items():
        add("same_logo", key, v)

    # method 11: this restaurant's branchName == another restaurant's name
    for bb, v in idx_bnbrand.items():
        add("branchname_matches_other_name", bb, v)

    # method 12: prefix -- one full name starts with another restaurant's name
    for _id, r in rec.items():
        t = tokens(first_part(r["name"]))
        hit = None
        for L in range(len(t) - 1, 0, -1):
            head = " ".join(t[:L])
            if len(head.replace(" ", "")) < MIN_PREFIX_LEN:
                break
            if head in idx_altbrand:
                others = [x for x in idx_altbrand[head] if x != _id]
                if others:
                    hit = head
                    break
        if hit:
            add("name_is_prefix_of_other", hit, [_id] + idx_altbrand[hit])

    # method 13: fuzzy spelling -- compare inside blocks of first 3 letters
    blocks = defaultdict(list)
    for _id, r in rec.items():
        s = squash(r["name"])
        if s:
            blocks[s[:3]].append(_id)

    skipped = 0
    fuzzy_pairs = 0
    for blk, members in blocks.items():
        if len(members) > MAX_BLOCK_SIZE:
            skipped += 1
            continue
        for i in range(len(members)):
            a = members[i]
            sa = squash(rec[a]["name"])
            for j in range(i + 1, len(members)):
                b = members[j]
                sb = squash(rec[b]["name"])
                if sa == sb:
                    continue
                if abs(len(sa) - len(sb)) > 0.25 * max(len(sa), len(sb), 1):
                    continue
                sm = difflib.SequenceMatcher(None, sa, sb)
                if sm.real_quick_ratio() < FUZZY_MIN_RATIO:
                    continue
                if sm.ratio() >= FUZZY_MIN_RATIO:
                    add("fuzzy_spelling", blk, [a, b])
                    fuzzy_pairs += 1

    # ---------------------------------------------------------------
    # STEP 5: cluster_label for review only (union-find -- NOT merging)
    # ---------------------------------------------------------------
    flagged = sorted(reasons.keys())
    parent = {x: x for x in flagged}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # if two ids share any method:key -> same review cluster (display only)
    key_to_ids = defaultdict(set)
    for _id in flagged:
        for m in reasons[_id]:
            key_to_ids[m].add(_id)
    for _id_list in key_to_ids.values():
        _id_list = sorted(_id_list)
        for other in _id_list[1:]:
            ra, rb = find(_id_list[0]), find(other)
            if ra != rb:
                parent[rb] = ra

    roots = sorted({find(x) for x in flagged})
    label = {root: f"C{i+1:05d}" for i, root in enumerate(roots)}

    # ---------------------------------------------------------------
    # STEP 6: write the CSV
    # ---------------------------------------------------------------
    rows = []
    for _id in flagged:
        r = rec[_id]
        rows.append({
            "cluster_label": label[find(_id)],
            "id": _id,
            "name": r["name"],
            "restaurantSlug": r["restaurantSlug"] or "",
            "branchUrl": r["branchUrl"] or "",
            "branchName": r["branchName"] or "",
            "grouping_reason": " | ".join(sorted(reasons[_id])),
            "shopCity": r["shopCity"] or "",
            "logo": r["logo"] or "",
        })

    rows.sort(key=lambda x: (x["cluster_label"], x["name"].lower()))

    with open(OUT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "cluster_label", "id", "name", "restaurantSlug",
            "branchUrl", "branchName", "grouping_reason",
            "shopCity", "logo",
        ])
        w.writeheader()
        w.writerows(rows)

    # ---------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------
    print("\n================ SIMILARITY CANDIDATES ================")
    print(f"Ids flagged with >=1 similarity reason : {len(flagged)}")
    print(f"Review clusters (label only, no merge) : {len(roots)}")
    print(f"Fuzzy pairs found                      : {fuzzy_pairs}")
    print(f"Fuzzy blocks skipped (too big)         : {skipped}")

    method_counts = Counter()
    for _id in flagged:
        for m in reasons[_id]:
            method_counts[m.split("[")[0]] += 1
    print("\nIds touched per method:")
    for m, c in method_counts.most_common():
        print(f"  {m:<35}: {c}")

    print(f"\nSaved -> {OUT_FILE}")
    print("Sort by cluster_label in Excel to see candidates next to each other.")


if __name__ == "__main__":
    main()