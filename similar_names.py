# -*- coding: utf-8 -*-
"""
similar_names.py
----------------
Finds ids whose names MIGHT be the same restaurant, for manual validation.

* NO initial grouping: every id is compared as it is (all its names are used).
* NO size limits: a group can contain 70+ ids (Starbucks, McDonald's ...).
* Everything goes in ONE csv (strong + weak together).
* The logo is only shown as a link. It is NOT used to decide similarity.

Methods (same ideas as name_audit.py, applied per id)
    same_brand           same text before the first comma
    separator_rule       same text before a comma / "(" / " - " / "|"
    same_text_no_space   same letters ignoring spaces & punctuation (Mc Donalds = McDonalds)
    prefix_rule          the name starts with the name of an "established" id
    branchname_evidence  the branchName of its branches starts with another id's name
    similar_spelling     spelling similarity >= FUZZY_MIN_SCORE (all vs all, nothing skipped)
    noise_words_rule     same name after removing trailing generic words learned from the data
    same_first_word      same first word (always weak)

Strength
    strong = confidence "high"      weak = confidence "medium" / "low"

Output columns
    group_word      the word / text the grouping is based on
    group_method    which method created that group
    strength        strong / weak (of group_word)
    confidence      high / medium / low (of group_word)
    group_size      how many ids share this group_word + method
    ...             id, names, cuisineString, verticalType, city, branch info, logo
    strong_reason / weak_reason / grouping_reason   every method[word] the id was flagged by

Run
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
READ_WORKERS = int(os.environ.get("READ_WORKERS", "8"))
#LOGO_STRIP_QUERY = os.environ.get("LOGO_STRIP_QUERY", "0") == "1"  # drop ?width=180
LOGO_STRIP_QUERY = os.environ.get("LOGO_STRIP_QUERY", "1") == "1"  # drop ?width=180


# ---------------- thresholds (same meaning as name_audit) -------
EST_MIN_NAMES = 2                # "established" id: at least 2 names
EST_MIN_BRANCHES = 3             # or at least 3 branches
MIN_HEAD_LEN = 4                 # min letters of the name used as prefix head
HIGH_MIN_JACCARD = 0.5           # cuisine similarity for high confidence
MED_MIN_JACCARD = 0.3            # cuisine similarity for medium confidence
HIGH_MAX_TAIL_TOKENS = 6         # max words after the head for high confidence
SQUASH_MIN_JACCARD = 0.3         # cuisine similarity for same-text / spelling = high
FUZZY_MIN_SCORE = float(os.environ.get("FUZZY_MIN_SCORE", "88"))  # 0-100
FUZZY_CHUNK = 500
MIN_FIRST_TOKEN_LEN = 3          # min length of the first word
NOISE_CHECK_MIN_GROUPS = 3       # min ids ending with a word to evaluate it
NOISE_MIN_GROUPS = 8             # noise word: ends 8+ ids ...
NOISE_MAX_TOP_CUISINE_SHARE = 0.5  # ... and top cuisine share < 50%
BN_HIGH_MIN_BRANCHES = 2         # branchName evidence: high if 2+ branches
BN_HIGH_MIN_SHARE = 0.5          # or half of the id's branches

CONF_RANK = {"high": 0, "medium": 1, "low": 2}
METHOD_PRIORITY = {
    "same_brand": 0, "same_text_no_space": 1, "separator_rule": 2,
    "prefix_rule": 3, "branchname_evidence": 4, "similar_spelling": 5,
    "noise_words_rule": 6, "same_first_word": 7,
}

# ---------------------------------------------------------------
# TEXT NORMALIZATION (English + Arabic)
# ---------------------------------------------------------------
APOSTROPHES = re.compile(r"['`´‘’ʼ′ʻ]")
AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_AR_MAP = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ی": "ي", "ئ": "ي",
    "ة": "ه", "ؤ": "و", "ک": "ك",
    "ـ": None,
}
for _i, _d in enumerate("٠١٢٣٤٥٦٧٨٩"):
    _AR_MAP[_d] = str(_i)
for _i, _d in enumerate("۰۱۲۳۴۵۶۷۸۹"):
    _AR_MAP[_d] = str(_i)
AR_TABLE = str.maketrans(_AR_MAP)

SEP_RE = re.compile(r"\s*(?:,|،|\||\(|\)|\s[-–—]\s)\s*")
COMMA_RE = re.compile(r"[,،]")


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def norm_code(value):
    """1 and 1.0 and '1' all become '1'."""
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


def sort_key(value):
    value = str(value)
    if value.isdigit():
        return (0, int(value), value)
    return (1, 0, value)


def normalize_text(text):
    text = unicodedata.normalize("NFKC", str(text))
    text = APOSTROPHES.sub("", text)
    text = AR_DIACRITICS.sub("", text).translate(AR_TABLE)
    return text.lower()


def _strip_al(tok):
    if len(tok) > 4 and tok.startswith("ال"):
        return tok[2:]
    return tok


def tokens(text):
    return [_strip_al(t) for t in re.findall(r"[^\W_]+", normalize_text(text))]


def first_segment(name):
    """Text before the first comma."""
    return COMMA_RE.split(str(name), maxsplit=1)[0].strip()


def first_part(name):
    """Text before a comma / parenthesis / ' - ' / '|'."""
    parts = [p for p in SEP_RE.split(str(name)) if p.strip()]
    return parts[0].strip() if parts else str(name).strip()


def brand_key(name):
    return " ".join(tokens(first_segment(name)))


def alt_key(name):
    return " ".join(tokens(first_part(name)))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
# FUZZY (rapidfuzz, all vs all, nothing skipped)
# ---------------------------------------------------------------
def fuzzy_pairs(keys_in):
    keys = sorted(keys_in)
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
            if int(c) > gi:
                pairs.append((keys[gi], keys[int(c)], int(scores[r, c])))
    return pairs


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main(s3=None, bucket=None):
    if s3 is None:
        s3 = make_client()
        bucket = os.environ["CF_R2_BUCKET_NAME"]

    # ============ read + count ============
    files = list_json_files(s3, bucket, R2_PREFIX)
    print(f"Files found: {len(files)}")

    def new_acc():
        return {
            "names": Counter(), "branch_ids": set(), "cities": set(),
            "cuisines": set(), "cuisine_str": Counter(),
            "vertical": Counter(), "slug": Counter(), "url": Counter(),
            "branchName": Counter(), "shopCity": Counter(),
            "logo": Counter(), "branch_brands": defaultdict(set),
        }

    acc = defaultdict(new_acc)
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
                a = acc[_id]
                name = clean(r.get("name"))
                b_id = clean(r.get("branchId"))
                b_name = clean(r.get("branchName"))
                city = norm_code(r.get("shopCity"))
                cuisine_str = clean(r.get("cuisineString"))
                vertical = clean(r.get("verticalType"))

                if name:
                    a["names"][name] += 1
                if b_id:
                    a["branch_ids"].add(b_id)
                if city:
                    a["cities"].add(city)
                    a["shopCity"][city] += 1
                if cuisine_str:
                    a["cuisine_str"][cuisine_str] += 1
                    for part in cuisine_str.split(","):
                        p = re.sub(r"\s+", " ", part).strip().lower()
                        if p:
                            a["cuisines"].add(p)
                if vertical:
                    a["vertical"][vertical] += 1
                for field, bag in (("restaurantSlug", "slug"),
                                   ("branchUrl", "url"), ("logo", "logo")):
                    v = clean(r.get(field))
                    if v:
                        a[bag][v] += 1
                if b_name:
                    a["branchName"][b_name] += 1
                    if b_id:
                        bk = brand_key(b_name)
                        if bk:
                            a["branch_brands"][bk].add(b_id)
            print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    print("\n================ COUNTS ================")
    print(f"Total records (all files) : {total_records}")
    print(f"Unique ids after dedup    : {len(acc)}")

    # ============ one record per id ============
    def ranked(counter):
        return [v for v, _ in sorted(counter.items(),
                                     key=lambda x: (-x[1], len(x[0]), x[0]))]

    def canon(counter):
        r = ranked(counter)
        return r[0] if r else None

    rec = {}
    for _id, a in acc.items():
        if not a["names"]:
            continue
        names = ranked(a["names"])
        keys = {brand_key(n) for n in names}
        keys.discard("")
        logo = canon(a["logo"])
        if logo and LOGO_STRIP_QUERY:
            logo = logo.split("?")[0]
        rec[_id] = {
            "id": _id,
            "name": names[0],
            "names": names,
            "keys": keys,
            "no_comma": all(not COMMA_RE.search(n) for n in names),
            "branch_ids": a["branch_ids"],
            "cities": a["cities"],
            "cuisines": a["cuisines"],
            "branch_brands": a["branch_brands"],
            "cuisineString": canon(a["cuisine_str"]),
            "verticalType": canon(a["vertical"]),
            "restaurantSlug": canon(a["slug"]),
            "branchUrl": canon(a["url"]),
            "branchName": canon(a["branchName"]),
            "shopCity": canon(a["shopCity"]),
            "logo": logo,
        }
    print(f"Unique ids with a name    : {len(rec)}")

    def established(r):
        return (len(r["names"]) >= EST_MIN_NAMES
                or len(r["branch_ids"]) >= EST_MIN_BRANCHES)

    # ============ reasons container ============
    # reasons[id] = list of (method, word, confidence, group_size)
    reasons = defaultdict(list)

    def add_group(method, word, id_conf):
        """id_conf: {id: confidence}"""
        if len(id_conf) < 2 or not word:
            return
        size = len(id_conf)
        for _id, c in id_conf.items():
            reasons[_id].append((method, word, c, size))

    def set_best(d, _id, conf):
        if _id not in d or CONF_RANK[conf] < CONF_RANK[d[_id]]:
            d[_id] = conf

    # ============ noise words learned from the data ============
    tail_ids = defaultdict(set)
    for _id, r in rec.items():
        for n in r["names"]:
            t = tokens(first_part(n))
            if len(t) >= 2:
                tail_ids[t[-1]].add(_id)

    noise_set, noise_info = set(), []
    for tok, idset in tail_ids.items():
        if len(idset) < NOISE_CHECK_MIN_GROUPS:
            continue
        cc = Counter()
        for i in idset:
            cc.update(rec[i]["cuisines"])
        top_c, top_n = cc.most_common(1)[0] if cc else (None, 0)
        share = top_n / len(idset)
        if len(idset) >= NOISE_MIN_GROUPS and share < NOISE_MAX_TOP_CUISINE_SHARE:
            noise_set.add(tok)
            noise_info.append((tok, len(idset), top_c, round(share, 2)))
    noise_info.sort(key=lambda x: (-x[1], x[0]))

    # ============ indexes ============
    brand_idx = defaultdict(set)      # text before first comma
    alt_idx = defaultdict(set)        # text before comma / ( / - / |
    noise_idx = defaultdict(set)      # alt text without trailing noise words
    squash_idx = defaultdict(set)     # brand text without spaces
    squash_keys = defaultdict(set)    # squash -> distinct brand texts
    first_idx = defaultdict(set)      # first word of brand text

    for _id, r in rec.items():
        for n in r["names"]:
            bk = brand_key(n)
            if bk:
                brand_idx[bk].add(_id)
                sq = bk.replace(" ", "")
                squash_idx[sq].add(_id)
                squash_keys[sq].add(bk)
                w = bk.split()[0]
                if len(w) >= MIN_FIRST_TOKEN_LEN:
                    first_idx[w].add(_id)
            ak = alt_key(n)
            if ak:
                alt_idx[ak].add(_id)
                t = ak.split()
                while len(t) > 1 and t[-1] in noise_set:
                    t.pop()
                noise_idx[" ".join(t)].add(_id)

    # ============ name-based groups (deduped by same id-set) ============
    seen_sets = set()

    def name_group(method, word, idset, conf):
        if len(idset) < 2:
            return
        fs = frozenset(idset)
        if fs in seen_sets:
            return
        seen_sets.add(fs)
        add_group(method, word, dict.fromkeys(idset, conf))

    for key, s in brand_idx.items():
        name_group("same_brand", key, s, "high")

    for key, s in alt_idx.items():
        name_group("separator_rule", key, s, "high")

    for sq, s in squash_idx.items():
        keys = squash_keys[sq]
        if len(keys) < 2:
            continue                      # same text -> already same_brand
        ordered = sorted(s, key=lambda i: (-len(rec[i]["names"]), sort_key(i)))
        head = rec[ordered[0]]
        min_j = min(jaccard(head["cuisines"], rec[i]["cuisines"])
                    for i in ordered[1:])
        conf = "high" if min_j >= SQUASH_MIN_JACCARD else "low"
        name_group("same_text_no_space", " ~ ".join(sorted(keys)), s, conf)

    for key, s in noise_idx.items():
        name_group("noise_words_rule", key, s, "medium")

    # ============ same first word (weak) ============
    for key, s in first_idx.items():
        add_group("same_first_word", key, dict.fromkeys(s, "low"))

    # ============ prefix rule ============
    head_index = defaultdict(set)
    for _id, r in rec.items():
        if not established(r):
            continue
        for key in r["keys"]:
            t = tuple(key.split())
            if t and len("".join(t)) >= MIN_HEAD_LEN:
                head_index[t].add(_id)

    prefix_groups = defaultdict(dict)
    for _id, r in rec.items():
        best = None
        for key in r["keys"]:
            toks = key.split()
            for L in range(len(toks) - 1, 0, -1):
                heads = head_index.get(tuple(toks[:L]), set()) - {_id}
                if heads:
                    if best is None or L > best[0]:
                        best = (L, toks, heads)
                    break
        if best is None:
            continue
        L, toks, heads = best
        hi = sorted(heads, key=lambda h: (-len(rec[h]["names"]), sort_key(h)))[0]
        h = rec[hi]
        cj = jaccard(r["cuisines"], h["cuisines"])
        shared_cities = len(r["cities"] & h["cities"])
        tail = toks[L:]
        est = established(r)
        if (not est and r["no_comma"] and cj >= HIGH_MIN_JACCARD
                and shared_cities >= 1 and len(tail) <= HIGH_MAX_TAIL_TOKENS):
            conf = "high"
        elif not est and cj >= MED_MIN_JACCARD:
            conf = "medium"
        else:
            conf = "low"
        grp = prefix_groups[" ".join(toks[:L])]
        set_best(grp, _id, conf)
        for hd in heads:
            set_best(grp, hd, conf)
    for word, id_conf in prefix_groups.items():
        add_group("prefix_rule", word, id_conf)

    # ============ branchName evidence ============
    key_to_ids = defaultdict(set)
    for _id, r in rec.items():
        for k in r["keys"]:
            key_to_ids[k].add(_id)

    bn_groups = defaultdict(dict)
    for _id, r in rec.items():
        for bk, bset in r["branch_brands"].items():
            if bk in r["keys"]:
                continue                  # branchName = its own name: natural
            heads = key_to_ids.get(bk, set()) - {_id}
            if not heads:
                continue
            ev = len(bset)
            share = ev / max(1, len(r["branch_ids"]))
            conf = ("high" if ev >= BN_HIGH_MIN_BRANCHES
                    or share >= BN_HIGH_MIN_SHARE else "medium")
            grp = bn_groups[bk]
            set_best(grp, _id, conf)
            for hd in heads:
                set_best(grp, hd, conf)
    for word, id_conf in bn_groups.items():
        add_group("branchname_evidence", word, id_conf)

    # ============ similar spelling (all vs all) ============
    print(f"\nFuzzy: comparing {len(squash_idx)} unique names "
          f"(score >= {FUZZY_MIN_SCORE:.0f}), nothing skipped...")
    fz = fuzzy_pairs(squash_idx.keys())
    print(f"Fuzzy pairs found: {len(fz)}")
    for ka, kb, score in fz:
        ia, ib = squash_idx[ka], squash_idx[kb]
        ca = set().union(*(rec[i]["cuisines"] for i in ia))
        cb = set().union(*(rec[i]["cuisines"] for i in ib))
        conf = "high" if jaccard(ca, cb) >= SQUASH_MIN_JACCARD else "low"
        word = f"{sorted(squash_keys[ka])[0]} ~ {sorted(squash_keys[kb])[0]}"
        add_group("similar_spelling", word, dict.fromkeys(ia | ib, conf))

    # ============ build the rows ============
    rows = []
    for _id, rs in reasons.items():
        r = rec[_id]
        primary = min(rs, key=lambda x: (CONF_RANK[x[2]],
                                         METHOD_PRIORITY[x[0]], -x[3], x[1]))
        method, word, conf, size = primary
        tags_all = {f"{m}[{w}]" for m, w, c, s in rs}
        tags_strong = {f"{m}[{w}]" for m, w, c, s in rs if c == "high"}
        tags_weak = tags_all - tags_strong
        rows.append({
            "group_word": word,
            "group_method": method,
            "strength": "strong" if conf == "high" else "weak",
            "confidence": conf,
            "group_size": size,
            "id": _id,
            "name": r["name"],
            "all_names": " | ".join(r["names"]) if len(r["names"]) > 1 else "",
            "cuisineString": r["cuisineString"] or "",
            "verticalType": r["verticalType"] or "",
            "shopCity": r["shopCity"] or "",
            "branches": len(r["branch_ids"]),
            "restaurantSlug": r["restaurantSlug"] or "",
            "branchUrl": r["branchUrl"] or "",
            "branchName": r["branchName"] or "",
            "logo": r["logo"] or "",
            "strong_reason": " | ".join(sorted(tags_strong)),
            "weak_reason": " | ".join(sorted(tags_weak)),
            "grouping_reason": " | ".join(sorted(tags_all)),
        })

    rows.sort(key=lambda x: (x["group_word"], METHOD_PRIORITY[x["group_method"]],
                             x["name"].lower()))

    fields = ["group_word", "group_method", "strength", "confidence",
              "group_size", "id", "name", "all_names", "cuisineString",
              "verticalType", "shopCity", "branches", "restaurantSlug",
              "branchUrl", "branchName", "logo", "strong_reason",
              "weak_reason", "grouping_reason"]
    with open(OUT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # ============ summary ============
    strong_ids = sum(1 for x in rows if x["strength"] == "strong")
    print("\n================ SIMILARITY CANDIDATES ================")
    print(f"Ids in CSV                    : {len(rows)}")
    print(f"  strong (has a high reason)  : {strong_ids}")
    print(f"  weak only                   : {len(rows) - strong_ids}")

    per_method = defaultdict(lambda: {"ids": set(), "strong": set(), "groups": set()})
    for _id, rs in reasons.items():
        for m, w, c, s in rs:
            per_method[m]["ids"].add(_id)
            per_method[m]["groups"].add(w)
            if c == "high":
                per_method[m]["strong"].add(_id)
    print("\nPer method (ids touched | of them strong | groups):")
    for m in sorted(per_method, key=lambda x: METHOD_PRIORITY[x]):
        d = per_method[m]
        print(f"  {m:<22}: {len(d['ids']):>5} | {len(d['strong']):>5} | {len(d['groups'])}")

    biggest = sorted({(m, w, s) for rs in reasons.values() for m, w, c, s in rs},
                     key=lambda x: -x[2])[:10]
    print("\nLargest groups:")
    for m, w, s in biggest:
        print(f"  {m}[{w}] : {s} ids")

    print(f"\nNoise words learned from the data ({len(noise_info)}):")
    for tok, n, top_c, share in noise_info[:15]:
        print(f"  {tok}: ends {n} ids | top cuisine {top_c} {share}")

    print(f"\nSaved -> {OUT_FILE}")
    print("Sort / filter by group_word and strength in Excel; logo column has the link.")


if __name__ == "__main__":
    main()