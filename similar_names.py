# -*- coding: utf-8 -*-
"""
similar_names.py
----------------
1) Reads all JSON files from R2 under a prefix (in parallel)
2) Prints the record count of every file + the total
3) Aggregates all records per unique id (keeps ALL distinct names per id)
4) Flags ids that look like duplicates of other ids (candidates only --
   nothing is merged) and writes a CSV for manual validation.

Signals
-------
ALWAYS-union (strong, safe even for big groups):
    same_name, same_name_no_space, same_restaurant_slug, same_branch_url_slug
CAPPED-union (strong, but only unioned when the group is <= UNION_MAX_GROUP,
otherwise the reason is still written with "too_big" but clusters are NOT merged):
    same_brand, same_brand_minus_noise, same_logo_exact, same_logo_brand,
    branchname_matches_other_name, name_is_prefix_of_other, fuzzy_spelling
WEAK (reason only, never merges clusters, ids with only weak reasons are
hidden unless INCLUDE_WEAK_ONLY=1):
    same_first_word

Logo cleaning
-------------
logo_key(url) strips ?width=180, decodes %d8..., drops timestamps/digits and
generic words (logo, WhatsApp_Image, IMG, Capture, renditionDownload ...).
If nothing brand-like is left, the logo is ignored as a similarity signal.

Run
---
    export CF_R2_ENDPOINT_URL=... CF_R2_ACCESS_KEY_ID=... \
           CF_R2_SECRET_ACCESS_KEY=... CF_R2_BUCKET_NAME=...
    pip install boto3 rapidfuzz numpy
    python similar_names.py
"""

import os
import re
import csv
import json
import unicodedata
import urllib.parse
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import boto3
from botocore.config import Config
from rapidfuzz import fuzz, process

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
R2_PREFIX = os.environ.get(
    "R2_PREFIX", "merged-restaurant-info/year=2025/month=09/day=17/"
)
OUT_FILE = os.environ.get("OUT_FILE", "similar_name_candidates.csv")
LOGO_AUDIT_FILE = os.environ.get("LOGO_AUDIT_FILE", "logo_audit.csv")

READ_WORKERS = int(os.environ.get("READ_WORKERS", "8"))
FUZZY_MIN_SCORE = float(os.environ.get("FUZZY_MIN_SCORE", "88"))  # 0-100
FUZZY_MIN_LEN = int(os.environ.get("FUZZY_MIN_LEN", "5"))   # skip very short names
FUZZY_CHUNK = 500
UNION_MAX_GROUP = int(os.environ.get("UNION_MAX_GROUP", "40"))
INCLUDE_WEAK_ONLY = os.environ.get("INCLUDE_WEAK_ONLY", "0") == "1"
MAX_NAMES_PER_ID = 5
MIN_FIRST_WORD = 3        # min length of first word for same_first_word
MIN_PREFIX_LEN = 4        # min chars of a prefix for name_is_prefix_of_other
LOGO_MIN_KEY_LEN = 3      # min chars of a cleaned logo key

# method -> how it behaves
ALWAYS, CAPPED, WEAK = "always", "capped", "weak"
METHOD_KIND = {
    "same_name": ALWAYS,
    "same_name_no_space": ALWAYS,
    "same_restaurant_slug": ALWAYS,
    "same_branch_url_slug": ALWAYS,
    "same_brand": CAPPED,
    "same_brand_minus_noise": CAPPED,
    "same_logo_exact": CAPPED,
    "same_logo_brand": CAPPED,
    "branchname_matches_other_name": CAPPED,
    "fuzzy_spelling": CAPPED,
    "same_first_word": WEAK,
    "name_is_prefix_of_other": CAPPED,
}
# name-based methods: skip a group if an earlier method already produced
# exactly the same set of ids (removes duplicated reasons)
NAME_METHODS = ["same_name", "same_name_no_space", "same_brand",
                "same_brand_minus_noise"]

# trailing words that are not part of the brand (already normalized form)
NOISE_WORDS = {
    "mall", "branch", "branches", "coop", "city", "center", "centre",
    "tower", "complex", "plaza", "block", "avenue", "ave", "road", "st",
    "street", "area", "kuwait", "kw", "q8",
    "فرع", "فروع", "مجمع", "برج", "شارع", "منطقه", "كويت",
}

# words in logo file names that say nothing about the brand
GENERIC_LOGO_WORDS = {
    "logo", "logos", "img", "image", "images", "whatsapp", "screenshot",
    "capture", "photo", "pic", "pdf", "rendition", "renditiondownload",
    "download", "downloaded", "eng", "en", "ar", "arabic", "english",
    "new", "old", "final", "copy", "option", "converted", "resized",
    "resize", "thumb", "thumbnail", "medium", "small", "large", "square",
    "full", "with", "of", "the", "a", "page", "jpg", "jpeg", "png", "webp",
    "shop", "bg", "dh", "untitled", "design", "banner", "cover", "icon",
    "profile", "edit", "edited", "tmp", "mediumsquarelogo", "location",
    "pic",
}
LOGO_AFFIXES = ("logos", "logo", "jpeg", "jpg", "png")

# ---------------------------------------------------------------
# TEXT NORMALIZATION (English + Arabic)
# ---------------------------------------------------------------
APOSTROPHES = re.compile(r"['`´‘’ʼ′ʻ]")
AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_AR_MAP = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ی": "ي", "ئ": "ي",
    "ة": "ه", "ؤ": "و", "ک": "ك",
    "ـ": None,  # tatweel
}
for _i, _d in enumerate("٠١٢٣٤٥٦٧٨٩"):
    _AR_MAP[_d] = str(_i)
for _i, _d in enumerate("۰۱۲۳۴۵۶۷۸۹"):
    _AR_MAP[_d] = str(_i)
AR_TABLE = str.maketrans(_AR_MAP)

SEP_RE = re.compile(r"\s*(?:,|،|\||\(|\)|\s[-–—]\s)\s*")


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
    text = AR_DIACRITICS.sub("", text).translate(AR_TABLE)
    return text.lower()


def _strip_al(tok):
    """Remove the Arabic definite article from long tokens (الباشا -> باشا)."""
    if len(tok) > 4 and tok.startswith("ال"):
        return tok[2:]
    return tok


def tokens(text):
    toks = re.findall(r"[^\W_]+", normalize_text(text))
    return [_strip_al(t) for t in toks]


def first_part(name):
    """First non-empty part before a comma / parenthesis / dash / |."""
    parts = [p for p in SEP_RE.split(str(name)) if p.strip()]
    return parts[0].strip() if parts else str(name).strip()


def strip_noise_key(name):
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
    s = normalize_text(urllib.parse.unquote(m.group(1))).strip()
    return s or None


# ---------------------------------------------------------------
# LOGO CLEANING
# ---------------------------------------------------------------
def logo_exact(url):
    """Logo URL without query string (?width=180) -> exact-file signal."""
    if not url:
        return None
    return str(url).split("?")[0].split("#")[0].strip().lower() or None


def _strip_logo_affixes(word):
    changed = True
    while changed:
        changed = False
        for a in LOGO_AFFIXES:
            if word.startswith(a) and len(word) - len(a) >= 3:
                word, changed = word[len(a):], True
            elif word.endswith(a) and len(word) - len(a) >= 3:
                word, changed = word[:-len(a)], True
    return word


def logo_key(url):
    """
    Brand-like key from a logo file name, or None when the file name is
    generic (logo_6372..., WhatsApp_Image..., IMG_..., Capture...).
    Drops: query string, extension, all digits (timestamps), generic words.
    """
    if not url:
        return None
    path = str(url).split("?")[0].split("#")[0]
    fname = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    words = re.findall(r"[^\W\d_]+", normalize_text(stem))   # letters only
    out = []
    for w in words:
        if w in GENERIC_LOGO_WORDS:
            continue
        w = _strip_logo_affixes(w)
        if len(w) < 2 or w in GENERIC_LOGO_WORDS:
            continue
        out.append(_strip_al(w))
    key = " ".join(out)
    return key if len(key.replace(" ", "")) >= LOGO_MIN_KEY_LEN else None


# ---------------------------------------------------------------
# R2 IO
# ---------------------------------------------------------------
def make_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        config=Config(
            max_pool_connections=max(10, READ_WORKERS * 2),
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def list_json_files(s3, bucket, prefix):
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                files.append(obj["Key"])
    return sorted(files)


def load_records(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    data = json.loads(body)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "records", "items", "results"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return []


# ---------------------------------------------------------------
# FUZZY (rapidfuzz, all-vs-all in chunks, no blocking, no skipping)
# ---------------------------------------------------------------
def fuzzy_pairs(keys_in):
    keys = sorted(k for k in keys_in if len(k) >= FUZZY_MIN_LEN)
    pairs = []
    n = len(keys)
    for start in range(0, n, FUZZY_CHUNK):
        chunk = keys[start:start + FUZZY_CHUNK]
        scores = process.cdist(
            chunk, keys, scorer=fuzz.ratio,
            score_cutoff=FUZZY_MIN_SCORE, dtype=np.uint8, workers=-1,
        )
        rows, cols = np.nonzero(scores)
        for r, c in zip(rows, cols):
            gi = start + int(r)
            if int(c) > gi:                      # upper triangle only
                pairs.append((keys[gi], keys[int(c)], int(scores[r, c])))
    return pairs


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main(s3=None, bucket=None):
    if s3 is None:
        s3 = make_client()
        bucket = os.environ["CF_R2_BUCKET_NAME"]

    # ---------- STEP 1+2: list / read / count ----------
    files = list_json_files(s3, bucket, R2_PREFIX)
    print(f"Files found: {len(files)}")

    ids = defaultdict(lambda: defaultdict(Counter))
    total_records = 0

    def _load(k):
        return k, load_records(s3, bucket, k)

    with ThreadPoolExecutor(READ_WORKERS) as ex:
        for i, (key, records) in enumerate(ex.map(_load, files), start=1):
            total_records += len(records)
            for r in records:
                _id = clean(r.get("id"))
                if not _id:
                    continue
                bag = ids[_id]
                for field in ("name", "restaurantSlug", "branchUrl",
                              "branchName", "shopCity", "logo"):
                    v = r.get(field)
                    v = norm_code(v) if field == "shopCity" else clean(v)
                    if v:
                        bag[field][v] += 1
            print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    print("\n================ COUNTS ================")
    print(f"Total records (all files) : {total_records}")
    print(f"Unique ids after dedup    : {len(ids)}")

    # ---------- STEP 3: canonical row per id ----------
    def ranked(counter):
        # most frequent first, tie-break on shortest value
        return [v for v, _ in sorted(counter.items(),
                                     key=lambda x: (-x[1], len(x[0])))]

    def canon(counter):
        r = ranked(counter)
        return r[0] if r else None

    rec = {}
    for _id, bag in ids.items():
        names = ranked(bag["name"])
        if not names:
            continue     # cannot compare without a name
        logo = canon(bag["logo"])
        rec[_id] = {
            "id": _id,
            "name": names[0],
            "names": names[:MAX_NAMES_PER_ID],
            "restaurantSlug": canon(bag["restaurantSlug"]),
            "branchUrl": canon(bag["branchUrl"]),
            "branchName": canon(bag["branchName"]),
            "shopCity": canon(bag["shopCity"]),
            "logo": logo,
            "logo_exact": logo_exact(logo),
            "logo_key": logo_key(logo),
        }
    print(f"Unique ids with a name    : {len(rec)}")

    # ---------- logo audit ----------
    logo_stat = Counter()
    for r in rec.values():
        if not r["logo"]:
            logo_stat["no logo"] += 1
        elif r["logo_key"] is None:
            logo_stat["generic file name (ignored as brand signal)"] += 1
        else:
            logo_stat["brand-like file name (used)"] += 1
    print("\n================ LOGO CLEANING ================")
    for k, v in logo_stat.items():
        print(f"  {k:<48}: {v}")

    with open(LOGO_AUDIT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "logo_key", "logo"])
        for r in sorted(rec.values(), key=lambda x: (x["logo_key"] or "~", x["name"])):
            w.writerow([r["id"], r["name"], r["logo_key"] or "", r["logo"] or ""])

    # ---------- STEP 4: build indexes  method -> key -> {ids} ----------
    raw = defaultdict(lambda: defaultdict(set))

    def index(method, key, _id):
        if key:
            raw[method][key].add(_id)

    for _id, r in rec.items():
        for n in r["names"]:
            toks = tokens(n)
            if not toks:
                continue
            index("same_name", " ".join(toks), _id)
            index("same_name_no_space", "".join(toks), _id)
            ft = tokens(first_part(n))
            if ft:
                index("same_brand", " ".join(ft), _id)
            index("same_brand_minus_noise", strip_noise_key(n), _id)
            if len(toks[0]) >= MIN_FIRST_WORD:
                index("same_first_word", toks[0], _id)

        if r["restaurantSlug"]:
            index("same_restaurant_slug", normalize_text(r["restaurantSlug"]), _id)
        index("same_branch_url_slug", url_slug(r["branchUrl"]), _id)
        index("same_logo_exact", r["logo_exact"], _id)
        index("same_logo_brand", r["logo_key"], _id)

    brand_idx = raw["same_brand"]

    # branchName equals ANOTHER restaurant's brand
    for _id, r in rec.items():
        if not r["branchName"]:
            continue
        bn = " ".join(tokens(first_part(r["branchName"])))
        others = brand_idx.get(bn, set()) - {_id}
        if bn and others:
            grp = raw["branchname_matches_other_name"][bn]
            grp.add(_id)
            grp |= others

    # a name that starts with another restaurant's brand
    for _id, r in rec.items():
        for n in r["names"]:
            ft = tokens(first_part(n))
            hit = None
            for L in range(len(ft) - 1, 0, -1):
                head = " ".join(ft[:L])
                if len(head.replace(" ", "")) < MIN_PREFIX_LEN:
                    break
                if brand_idx.get(head, set()) - {_id}:
                    hit = head
                    break
            if hit:
                grp = raw["name_is_prefix_of_other"][hit]
                grp.add(_id)
                grp |= brand_idx[hit]
                break

    # ---------- turn indexes into groups ----------
    groups = []                      # (method, key, sorted ids)
    seen_name_sets = set()
    for method in NAME_METHODS:      # name methods first, deduped by id-set
        for key, idset in raw[method].items():
            if len(idset) < 2:
                continue
            fs = frozenset(idset)
            if fs in seen_name_sets:
                continue
            seen_name_sets.add(fs)
            groups.append((method, key, sorted(idset)))
    for method, by_key in raw.items():
        if method in NAME_METHODS or method == "fuzzy_spelling":
            continue
        for key, idset in by_key.items():
            if len(idset) >= 2:
                groups.append((method, key, sorted(idset)))

    # fuzzy spelling over the no-space keys (no blocking, nothing skipped)
    nospace = raw["same_name_no_space"]
    print(f"\nFuzzy: comparing {sum(1 for k in nospace if len(k) >= FUZZY_MIN_LEN)} "
          f"unique names (score >= {FUZZY_MIN_SCORE:.0f})...")
    fz = fuzzy_pairs(nospace.keys())
    for ka, kb, score in fz:
        idset = nospace[ka] | nospace[kb]
        if len(idset) >= 2:
            groups.append(("fuzzy_spelling", f"{ka}~{kb}:{score}", sorted(idset)))
    print(f"Fuzzy pairs found: {len(fz)}")

    # ---------- STEP 5: reasons, strength, review clusters ----------
    reasons = defaultdict(set)
    strong = set()
    city_peer = set()
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for method, key, id_list in groups:
        kind = METHOD_KIND[method]
        n = len(id_list)
        unionable = kind == ALWAYS or (kind == CAPPED and n <= UNION_MAX_GROUP)
        tag = f"{method}[{key}]"
        if kind == CAPPED and not unionable:
            tag += f"(n={n},too_big)"
        for _id in id_list:
            reasons[_id].add(tag)
        if not unionable:
            continue
        strong.update(id_list)
        root = find(id_list[0])
        for other in id_list[1:]:
            ro = find(other)
            if ro != root:
                parent[ro] = root
        cities = Counter(rec[i]["shopCity"] for i in id_list if rec[i]["shopCity"])
        for i in id_list:
            c = rec[i]["shopCity"]
            if c and cities[c] > 1:
                city_peer.add(i)

    flagged = [i for i in reasons if (i in strong or INCLUDE_WEAK_ONLY)]
    weak_only_hidden = len([i for i in reasons if i not in strong]) if not INCLUDE_WEAK_ONLY else 0

    cluster_members = defaultdict(list)
    for i in flagged:
        if i in strong:
            cluster_members[find(i)].append(i)
    order = sorted(cluster_members, key=lambda r_: (-len(cluster_members[r_]), r_))
    label = {root: f"C{k + 1:05d}" for k, root in enumerate(order)}

    # ---------- STEP 6: CSV ----------
    rows = []
    for _id in flagged:
        r = rec[_id]
        if _id in strong:
            root = find(_id)
            lab, size = label[root], len(cluster_members[root])
        else:
            lab, size = "WEAK", 1
        rows.append({
            "cluster_label": lab,
            "cluster_size": size,
            "id": _id,
            "name": r["name"],
            "all_names": " | ".join(r["names"]) if len(r["names"]) > 1 else "",
            "restaurantSlug": r["restaurantSlug"] or "",
            "branchUrl": r["branchUrl"] or "",
            "branchName": r["branchName"] or "",
            "shopCity": r["shopCity"] or "",
            "same_city_peer": "Y" if _id in city_peer else "",
            "strength": "strong" if _id in strong else "weak_only",
            "logo_key": r["logo_key"] or "",
            "logo": r["logo"] or "",
            "grouping_reason": " | ".join(sorted(reasons[_id])),
        })
    rows.sort(key=lambda x: (x["cluster_label"], x["name"].lower()))

    fields = ["cluster_label", "cluster_size", "id", "name", "all_names",
              "restaurantSlug", "branchUrl", "branchName", "shopCity",
              "same_city_peer", "strength", "logo_key", "logo",
              "grouping_reason"]
    with open(OUT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # ---------- SUMMARY ----------
    print("\n================ SIMILARITY CANDIDATES ================")
    print(f"Ids in CSV                              : {len(rows)}")
    print(f"  strong ids                            : {len(strong & set(flagged))}")
    print(f"  weak-only ids hidden                  : {weak_only_hidden}")
    print(f"Review clusters (label only, no merge)  : {len(order)}")
    if order:
        sizes = [len(cluster_members[r_]) for r_ in order[:5]]
        print(f"Largest clusters                        : {sizes}")
        if sizes[0] > 200:
            print("  !! very large cluster: lower UNION_MAX_GROUP or check the "
                  "grouping_reason of that cluster")

    method_counts = Counter()
    for _id in flagged:
        for m in reasons[_id]:
            method_counts[m.split("[")[0]] += 1
    print("\nIds touched per method:")
    for m, c in method_counts.most_common():
        print(f"  {m:<32} [{METHOD_KIND[m]:<6}]: {c}")

    print(f"\nSaved -> {OUT_FILE}")
    print(f"Saved -> {LOGO_AUDIT_FILE}  (check logo_key column to validate the logo cleaning)")
    print("Sort by cluster_label in Excel to see candidates next to each other.")


if __name__ == "__main__":
    main()