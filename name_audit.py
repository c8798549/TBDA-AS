import os
import re
import json
import difflib
import unicodedata
from collections import defaultdict, Counter
from itertools import combinations

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)
SEARCH_TERMS = os.environ.get("AUDIT_SEARCH", "starbucks")

# ---------------- output files ----------------
SUMMARY_FILE = "name_audit.json"
PREFIX_FILE = "name_audit_prefix_candidates.json"
SIMILAR_FILE = "name_audit_similar_names.json"
SUSPICIOUS_FILE = "name_audit_suspicious_merges.json"
PATTERNS_FILE = "name_audit_name_patterns.json"
SEARCH_FILE = "name_audit_search.json"

# ---------------- thresholds (عدّليهم من هنا) ----------------
EST_MIN_NAMES = 2                # مطعم "مؤكد": اسمين على الأقل
EST_MIN_BRANCHES = 3             # أو 3 فروع على الأقل
MIN_HEAD_LEN = 4                 # أقل طول (حروف) لاسم الأصل في قاعدة البادئة
HIGH_MIN_JACCARD = 0.5           # تشابه الأنواع لثقة high
MED_MIN_JACCARD = 0.3            # تشابه الأنواع لثقة medium
HIGH_MAX_TAIL_TOKENS = 6         # أقصى عدد كلمات بعد اسم الأصل لثقة high
SQUASH_MIN_JACCARD = 0.3         # تشابه الأنواع لاعتبار "نفس النص" ثقة high
FUZZY_MIN_RATIO = 0.88           # أقل تشابه إملائي
MAX_BLOCK_SIZE = 600             # أكبر بلوك بنقارن جواه
SUSPICIOUS_MAX_RATIO = 0.6       # دمج مشبوه لو التشابه أقل من كده
MIXED_MIN_IDS = 5                # أقل ids لفحص الأنواع المختلطة
MIXED_MAX_AVG_JACCARD = 0.3      # متوسط تشابه أقل من كده = أنواع مختلطة
MIXED_MAX_IDS_COMPARED = 40      # أقصى ids بنقارنهم في المجموعة

SAMPLES = 5                      # عدد الأمثلة المطبوعة
TOP_N_PRINT = 10
TOP_TOKENS = 25                  # عدد الكلمات الأخيرة الأكتر تكرارًا
BIG_GROUPS = 20
SEARCH_PRINT_MAX = 15

CONF_RANK = {"high": 0, "medium": 1, "low": 2}

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]

APOSTROPHES = re.compile(r"[\'`´‘’ʼ′ʻ]")


# ============================================================
# HELPERS
# ============================================================

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
    """يوحّد أكواد المدينة: 1 و 1.0 و "1" كلهم "1"."""
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


def first_segment(name):
    return name.split(",")[0].strip()


def brand_key(name):
    """نفس مفتاح التجميع بتاع market_insights."""
    seg = unicodedata.normalize("NFKC", first_segment(name))
    seg = APOSTROPHES.sub("", seg)
    seg = re.sub(r"\s+", " ", seg).strip().lower()
    return seg


def tokens(text):
    """كلمات النص (حروف وأرقام بس) بحروف صغيرة."""
    text = unicodedata.normalize("NFKC", text)
    text = APOSTROPHES.sub("", text)
    return re.findall(r"[^\W_]+", text.lower())


def squash(text):
    """النص من غير مسافات ولا علامات."""
    return "".join(tokens(text))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def safe_div(a, b, digits=2):
    return round(a / b, digits) if b else 0


def short_list(values, n=5):
    values = list(values)
    if len(values) <= n:
        return str(values)
    return str(values[:n])[:-1] + f", ... +{len(values) - n} more]"


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def strip_internal(entries):
    """يشيل الحقول الداخلية (اللي بتبدأ بـ _) قبل الحفظ."""
    return [
        {k: v for k, v in e.items() if not k.startswith("_")}
        for e in entries
    ]


def print_block(title, total, lines, filename=None):
    print(f"\n{title} ({total})")

    for line in lines:
        print(f"  {line}")

    if total > len(lines):
        print(f"  ... and {total - len(lines)} more")

    if filename:
        print(f"  -> full details: {filename}")


# ============================================================
# MAIN
# ============================================================

def main():
    # ----------------------------------------------------
    # list files
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

    # ----------------------------------------------------
    # containers
    # ----------------------------------------------------
    names = set()
    id_to_names = defaultdict(Counter)
    name_to_ids = defaultdict(set)
    id_to_branch_ids = defaultdict(set)
    branch_city = defaultdict(set)
    id_to_cuisines = defaultdict(set)

    total_records = 0

    # ----------------------------------------------------
    # process files
    # ----------------------------------------------------
    for i, key in enumerate(files, start=1):
        records = load_records(key)
        total_records += len(records)

        for r in records:
            _id = clean(r.get("id"))
            name = clean(r.get("name"))
            b_id = clean(r.get("branchId"))
            cuisine_str = clean(r.get("cuisineString"))
            city = norm_code(r.get("shopCity"))

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

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    # ----------------------------------------------------
    # التجميع الحالي (نفس قواعد market_insights)
    #   1) الجزء الأول من الاسم قبل أول فاصلة
    #   2) أي أسماء ظهرت مع نفس الـ id تتدمج
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
        if ra == rb:
            return
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
    for root in sorted(components):
        keys = sorted(components[root])

        g_names, g_ids = set(), set()
        variants = Counter()
        key_ids = {}
        for k in keys:
            g_names |= brand_to_names[k]
            g_ids |= brand_to_ids[k]
            variants.update(brand_variants[k])
            key_ids[k] = set(brand_to_ids[k])

        top_variant = sorted(
            variants.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

        bids, cuisines, cities = set(), set(), set()
        for rid in g_ids:
            bids.update(id_to_branch_ids.get(rid, ()))
            cuisines.update(id_to_cuisines.get(rid, ()))
        for bid in bids:
            cities.update(branch_city.get(bid, ()))

        key_cuisines = {}
        for k, kids in key_ids.items():
            cs = set()
            for rid in kids:
                cs.update(id_to_cuisines.get(rid, ()))
            key_cuisines[k] = cs

        groups.append({
            "root": root,
            "display": top_variant,
            "main_key": brand_key(top_variant),
            "main_squash": squash(top_variant),
            "keys": keys,
            "names": g_names,
            "ids": g_ids,
            "branches": bids,
            "cuisines": cuisines,
            "cities": cities,
            "variants": variants,
            "key_ids": key_ids,
            "key_cuisines": key_cuisines,
        })

    total_groups = len(groups)

    def established(g):
        return (
            len(g["names"]) >= EST_MIN_NAMES
            or len(g["branches"]) >= EST_MIN_BRANCHES
        )

    def no_comma(g):
        return all("," not in n for n in g["names"])

    def brief(g):
        return {
            "restaurant": g["display"],
            "names": len(g["names"]),
            "ids": len(g["ids"]),
            "branches": len(g["branches"]),
            "sample_names": sorted(g["names"])[:3],
        }

    # ====================================================
    # 1) تحليل شكل الأسماء
    # ====================================================
    all_names = sorted(names)
    names_with_comma = sum(1 for n in all_names if "," in n)
    names_without_comma = len(all_names) - names_with_comma
    names_with_dash = sum(1 for n in all_names if " - " in n)
    names_with_paren = sum(1 for n in all_names if "(" in n or ")" in n)

    segments_dist = Counter(
        min(len(n.split(",")), 5) for n in all_names
    )

    trailing = Counter()
    for n in all_names:
        t = tokens(n)
        if t:
            trailing[t[-1]] += 1
    top_trailing = [
        {"token": t, "names": c}
        for t, c in trailing.most_common(TOP_TOKENS)
    ]

    groups_merged_by_id = sum(1 for g in groups if len(g["keys"]) > 1)
    groups_single_name = sum(1 for g in groups if len(g["names"]) == 1)
    groups_no_comma = sum(1 for g in groups if no_comma(g))
    groups_established = sum(1 for g in groups if established(g))

    big_groups = [
        {
            **brief(g),
            "keys": len(g["keys"]),
            "name_variants": sorted(g["variants"].keys())[:10],
        }
        for g in sorted(
            groups, key=lambda g: (-len(g["ids"]), g["display"].lower())
        )[:BIG_GROUPS]
    ]

    patterns_file = {
        "unique_names": len(all_names),
        "names_with_comma": names_with_comma,
        "names_without_comma": names_without_comma,
        "names_with_dash_separator": names_with_dash,
        "names_with_parentheses": names_with_paren,
        "comma_segments_distribution": {
            (f"{k}+" if k == 5 else str(k)): v
            for k, v in sorted(segments_dist.items())
        },
        "most_common_last_words": top_trailing,
        "largest_groups_by_ids": big_groups,
    }

    # ====================================================
    # 2) تجميعات ناقصة: قاعدة البادئة
    # ====================================================
    head_index = defaultdict(list)
    for gi, g in enumerate(groups):
        if not established(g):
            continue
        for key in g["keys"]:
            t = tuple(tokens(key))
            if t and len("".join(t)) >= MIN_HEAD_LEN:
                if gi not in head_index[t]:
                    head_index[t].append(gi)

    prefix_entries = []
    for ki, g in enumerate(groups):
        best = None
        for key in g["keys"]:
            toks = tokens(key)
            for L in range(len(toks) - 1, 0, -1):
                heads = [
                    h for h in head_index.get(tuple(toks[:L]), ())
                    if h != ki
                ]
                if heads:
                    if best is None or L > best[0]:
                        best = (L, toks, heads)
                    break

        if best is None:
            continue

        L, toks, heads = best
        hi = max(heads, key=lambda h: (len(groups[h]["names"]), -h))
        h = groups[hi]

        cj = round(jaccard(g["cuisines"], h["cuisines"]), 2)
        shared_cities = len(g["cities"] & h["cities"])
        tail = toks[L:]
        nc = no_comma(g)

        if (
            not established(g)
            and nc
            and cj >= HIGH_MIN_JACCARD
            and shared_cities >= 1
            and len(tail) <= HIGH_MAX_TAIL_TOKENS
        ):
            conf = "high"
        elif not established(g) and cj >= MED_MIN_JACCARD:
            conf = "medium"
        else:
            conf = "low"

        prefix_entries.append({
            "confidence": conf,
            "restaurant": g["display"],
            "names": sorted(g["names"])[:5],
            "ids": len(g["ids"]),
            "branches": len(g["branches"]),
            "would_merge_into": h["display"],
            "head_names": len(h["names"]),
            "head_ids": len(h["ids"]),
            "head_branches": len(h["branches"]),
            "head_sample_names": sorted(h["names"])[:3],
            "matched_prefix": " ".join(toks[:L]),
            "tail": " ".join(tail),
            "all_names_without_comma": nc,
            "restaurant_is_established": established(g),
            "cuisine_similarity": cj,
            "shared_cities": shared_cities,
            "_k": ki,
            "_h": hi,
        })

    prefix_entries.sort(
        key=lambda e: (
            CONF_RANK[e["confidence"]], -e["head_names"],
            e["restaurant"].lower(),
        )
    )

    prefix_by_conf = Counter(e["confidence"] for e in prefix_entries)
    prefix_high_pairs = [
        (e["_k"], e["_h"]) for e in prefix_entries
        if e["confidence"] == "high"
    ]
    prefix_med_pairs = [
        (e["_k"], e["_h"]) for e in prefix_entries
        if e["confidence"] == "medium"
    ]

    # ====================================================
    # 3) تجميعات ناقصة: نفس النص من غير مسافات وعلامات
    # ====================================================
    squash_map = defaultdict(set)
    for gi, g in enumerate(groups):
        for key in g["keys"]:
            s = squash(key)
            if s:
                squash_map[s].add(gi)

    squash_entries = []
    squash_pairs = []
    for s, gis in sorted(squash_map.items()):
        if len(gis) < 2:
            continue
        ordered = sorted(
            gis, key=lambda gi: (-len(groups[gi]["names"]), gi)
        )
        first = groups[ordered[0]]
        min_j = min(
            jaccard(first["cuisines"], groups[gi]["cuisines"])
            for gi in ordered[1:]
        )
        conf = "high" if min_j >= SQUASH_MIN_JACCARD else "low"

        squash_entries.append({
            "confidence": conf,
            "same_text_without_spaces": s,
            "groups": [brief(groups[gi]) for gi in ordered],
            "min_cuisine_similarity": round(min_j, 2),
        })
        if conf == "high":
            for gi in ordered[1:]:
                squash_pairs.append((gi, ordered[0]))

    squash_entries.sort(
        key=lambda e: (CONF_RANK[e["confidence"]], e["same_text_without_spaces"])
    )

    # ====================================================
    # 4) تجميعات ناقصة: تشابه إملائي
    # ====================================================
    blocks = defaultdict(list)
    for gi, g in enumerate(groups):
        m = g["main_squash"]
        if m:
            blocks[m[:4]].append(gi)

    similar_entries = []
    skipped_blocks = 0
    for blk, members in blocks.items():
        if len(members) > MAX_BLOCK_SIZE:
            skipped_blocks += 1
            continue
        for x in range(len(members)):
            a = groups[members[x]]
            for y in range(x + 1, len(members)):
                b = groups[members[y]]
                sa, sb = a["main_squash"], b["main_squash"]
                if sa == sb:
                    continue
                if abs(len(sa) - len(sb)) > 0.25 * max(len(sa), len(sb)):
                    continue
                sm = difflib.SequenceMatcher(None, sa, sb)
                if (
                    sm.real_quick_ratio() < FUZZY_MIN_RATIO
                    or sm.quick_ratio() < FUZZY_MIN_RATIO
                ):
                    continue
                ratio = sm.ratio()
                if ratio < FUZZY_MIN_RATIO:
                    continue
                similar_entries.append({
                    "similarity": round(ratio, 3),
                    "restaurant_a": brief(a),
                    "restaurant_b": brief(b),
                    "cuisine_similarity": round(
                        jaccard(a["cuisines"], b["cuisines"]), 2
                    ),
                    "shared_cities": len(a["cities"] & b["cities"]),
                })

    similar_entries.sort(
        key=lambda e: (
            -e["similarity"], e["restaurant_a"]["restaurant"].lower()
        )
    )

    similar_file = {
        "thresholds": {
            "min_similarity": FUZZY_MIN_RATIO,
            "compared_within_groups_sharing_first_4_letters": True,
        },
        "blocks_skipped_too_big": skipped_blocks,
        "same_text_without_spaces": squash_entries,
        "similar_spelling_pairs": similar_entries,
    }

    # ====================================================
    # 5) دمج ممكن يكون غلط
    # ====================================================
    susp_by_id = []
    for g in groups:
        if len(g["keys"]) < 2:
            continue

        main = g["main_key"]
        if main not in g["key_cuisines"]:
            main = g["keys"][0]
        main_tokens = tokens(main)
        main_sq = squash(main)

        worst = None
        for k in g["keys"]:
            if k == main:
                continue
            k_tokens = tokens(k)
            n = min(len(main_tokens), len(k_tokens))
            if n and main_tokens[:n] == k_tokens[:n]:
                continue    # واحد بيبدأ بالتاني: دمج منطقي
            ratio = difflib.SequenceMatcher(None, main_sq, squash(k)).ratio()
            if worst is None or ratio < worst[0]:
                worst = (ratio, k)

        if worst and worst[0] < SUSPICIOUS_MAX_RATIO:
            other = worst[1]
            susp_by_id.append({
                "restaurant": g["display"],
                "similarity": round(worst[0], 2),
                "keys_merged": g["keys"],
                "most_different_pair": [main, other],
                "cuisine_similarity_of_pair": round(jaccard(
                    g["key_cuisines"].get(main, set()),
                    g["key_cuisines"].get(other, set()),
                ), 2),
                "shared_ids": sorted(
                    g["key_ids"].get(main, set())
                    & g["key_ids"].get(other, set()),
                    key=sort_key,
                )[:5],
                "sample_names": sorted(g["names"])[:6],
                "ids": len(g["ids"]),
            })

    susp_by_id.sort(key=lambda e: (e["similarity"], e["restaurant"].lower()))

    susp_mixed = []
    for g in groups:
        ids_with = [
            rid for rid in sorted(g["ids"], key=sort_key)
            if id_to_cuisines.get(rid)
        ][:MIXED_MAX_IDS_COMPARED]

        if len(g["ids"]) < MIXED_MIN_IDS or len(ids_with) < 2:
            continue

        sims = [
            jaccard(id_to_cuisines[a], id_to_cuisines[b])
            for a, b in combinations(ids_with, 2)
        ]
        avg = sum(sims) / len(sims)

        if avg < MIXED_MAX_AVG_JACCARD:
            top = Counter()
            for rid in ids_with:
                top.update(id_to_cuisines[rid])
            susp_mixed.append({
                "restaurant": g["display"],
                "ids": len(g["ids"]),
                "avg_cuisine_similarity_between_ids": round(avg, 2),
                "top_cuisines": [
                    {"cuisine": c, "ids": n} for c, n in top.most_common(5)
                ],
                "sample_names": sorted(g["names"])[:6],
            })

    susp_mixed.sort(
        key=lambda e: (
            e["avg_cuisine_similarity_between_ids"], e["restaurant"].lower()
        )
    )

    suspicious_file = {
        "thresholds": {
            "max_name_similarity": SUSPICIOUS_MAX_RATIO,
            "mixed_min_ids":