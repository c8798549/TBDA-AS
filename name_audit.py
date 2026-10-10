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
# بحث اختياري بالاسم (مفصول بفواصل). لو فاضي مفيش بحث.
SEARCH_TERMS = os.environ.get("AUDIT_SEARCH", "")

# ---------------- output files ----------------
SUMMARY_FILE = "name_audit.json"
PATTERNS_FILE = "name_audit_name_patterns.json"
FIRST_TOKEN_FILE = "name_audit_first_token_clusters.json"
PREFIX_FILE = "name_audit_prefix_candidates.json"
BRANCHNAME_FILE = "name_audit_branchname_evidence.json"
SIMILAR_FILE = "name_audit_similar_names.json"
SUSPICIOUS_FILE = "name_audit_suspicious_merges.json"
RULE_MERGES_FILE = "name_audit_rule_merges.json"
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

MIN_FIRST_TOKEN_LEN = 3          # أقل طول للكلمة الأولى في التجمعات
GENERIC_MIN_GROUPS = 25          # كلمة أولى بتبدأ أكتر من كده = عامة
NOISE_CHECK_MIN_GROUPS = 3       # أقل مطاعم لتسجيل كلمة آخر الاسم في التقرير
NOISE_MIN_GROUPS = 8             # كلمة زوايد: في 8 مطاعم أو أكتر
NOISE_MAX_TOP_CUISINE_SHARE = 0.5  # وأكتر نوع فيهم أقل من 50%
BN_HIGH_MIN_BRANCHES = 2         # دليل branchName: ثقة high لو فرعين أو أكتر
BN_HIGH_MIN_SHARE = 0.5          # أو نصف فروع المطعم

SAMPLES = 5                      # عدد الأمثلة المطبوعة
TOP_N_PRINT = 10
TOP_TOKENS = 25                  # عدد الكلمات الأكتر تكرارًا في الطباعة
BIG_GROUPS = 20
MAX_MEMBERS_SAVED = 25           # أقصى عدد مطاعم بتتحفظ جوه التجمع الواحد
MAX_CLUSTERS_SAVED = 300         # أقصى عدد تجمعات بتتحفظ لكل قاعدة
MAX_FIRST_TOKEN_SAVED = 500
SEARCH_PRINT_MAX = 15

CONF_RANK = {"high": 0, "medium": 1, "low": 2}

SEP_RE = re.compile(r"\s*(?:,|\(|\s[-–—]\s)\s*")

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


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text)
    text = APOSTROPHES.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def first_segment(name):
    return name.split(",")[0].strip()


def brand_key(name):
    """نفس مفتاح التجميع بتاع market_insights (قبل أول فاصلة)."""
    return normalize_text(first_segment(name))


def alt_first_part(name):
    """الجزء الأول قبل فاصلة أو ' - ' أو قوس."""
    return SEP_RE.split(name, maxsplit=1)[0].strip()


def alt_brand_key(name):
    return normalize_text(alt_first_part(name))


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

    # id -> {brand key من branchName -> {branchIds}}
    id_branch_brands = defaultdict(lambda: defaultdict(set))
    bk_example = {}                      # مثال branchName لكل brand key

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
            b_name = clean(r.get("branchName"))
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
            if _id and b_id and b_name:
                bk = brand_key(b_name)
                if bk:
                    id_branch_brands[_id][bk].add(b_id)
                    bk_example.setdefault(bk, b_name)

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
    key_to_group = {}

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
        branch_brands = defaultdict(set)
        for rid in g_ids:
            bids.update(id_to_branch_ids.get(rid, ()))
            cuisines.update(id_to_cuisines.get(rid, ()))
            for bk, bset in id_branch_brands.get(rid, {}).items():
                branch_brands[bk] |= bset
        for bid in bids:
            cities.update(branch_city.get(bid, ()))

        key_cuisines = {}
        for k, kids in key_ids.items():
            cs = set()
            for rid in kids:
                cs.update(id_to_cuisines.get(rid, ()))
            key_cuisines[k] = cs

        gi = len(groups)
        for k in keys:
            key_to_group[k] = gi

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
            "branch_brands": branch_brands,
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
    # أدوات التجميع والمحاكاة
    # ====================================================
    def clusters_from_pairs(pairs):
        p = list(range(total_groups))

        def f(x):
            while p[x] != x:
                p[x] = p[p[x]]
                x = p[x]
            return x

        touched = set()
        for a, b in pairs:
            ra, rb = f(a), f(b)
            touched.add(a)
            touched.add(b)
            if ra != rb:
                p[rb] = ra

        comp = defaultdict(list)
        for i in touched:
            comp[f(i)].append(i)
        return [sorted(v) for v in comp.values() if len(v) > 1]

    def count_after(pairs):
        removed = sum(len(c) - 1 for c in clusters_from_pairs(pairs))
        return total_groups - removed

    def pairs_from_map(mapping):
        pairs = []
        for _k, gis in mapping.items():
            gis = sorted(gis)
            for other in gis[1:]:
                pairs.append((gis[0], other))
        return pairs

    def cluster_entry(members):
        ms = sorted(
            members,
            key=lambda gi: (
                -len(groups[gi]["names"]), -len(groups[gi]["ids"]),
                groups[gi]["display"].lower(),
            ),
        )
        head = groups[ms[0]]
        sims = [
            jaccard(head["cuisines"], groups[gi]["cuisines"])
            for gi in ms[1:]
        ]
        all_branches = set()
        for gi in ms:
            all_branches |= groups[gi]["branches"]
        return {
            "restaurants_merged": len(ms),
            "result_name": head["display"],
            "total_names": sum(len(groups[gi]["names"]) for gi in ms),
            "total_ids": sum(len(groups[gi]["ids"]) for gi in ms),
            "total_branches": len(all_branches),
            "min_cuisine_similarity_to_largest": (
                round(min(sims), 2) if sims else None
            ),
            "members": [brief(groups[gi]) for gi in ms[:MAX_MEMBERS_SAVED]],
        }

    def rule_report(pairs):
        clusters = clusters_from_pairs(pairs)
        entries = [cluster_entry(c) for c in clusters]
        entries.sort(
            key=lambda e: (-e["restaurants_merged"], e["result_name"].lower())
        )
        removed = sum(len(c) - 1 for c in clusters)
        return {
            "clusters_count": len(entries),
            "restaurants_removed": removed,
            "restaurants_after_rule": total_groups - removed,
            "clusters": entries[:MAX_CLUSTERS_SAVED],
        }

    # ====================================================
    # 1) تحليل شكل الأسماء
    # ====================================================
    all_names = sorted(names)
    names_with_comma = sum(1 for n in all_names if "," in n)
    names_without_comma = len(all_names) - names_with_comma
    names_with_dash = sum(1 for n in all_names if " - " in n)
    names_with_paren = sum(1 for n in all_names if "(" in n or ")" in n)
    names_with_digits = sum(
        1 for n in all_names if any(ch.isdigit() for ch in n)
    )
    names_non_ascii = sum(
        1 for n in all_names if re.search(r"[^\x00-\x7F]", n)
    )
    names_all_caps = sum(1 for n in all_names if n.isupper())
    names_double_space = sum(1 for n in all_names if "  " in n)
    names_end_punct = sum(1 for n in all_names if n[-1] in "!.,-:;")
    names_very_short = sum(1 for n in all_names if len(squash(n)) <= 2)

    lower_map = defaultdict(set)
    for n in all_names:
        lower_map[n.lower()].add(n)
    names_case_variants = sum(1 for v in lower_map.values() if len(v) > 1)

    segments_dist = Counter(min(len(n.split(",")), 5) for n in all_names)

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

    # ====================================================
    # 2) كلمات الزوايد (من الداتا)
    # ====================================================
    tail_groups = defaultdict(set)
    for gi, g in enumerate(groups):
        for n in g["names"]:
            t = tokens(alt_first_part(n))
            if len(t) >= 2:
                tail_groups[t[-1]].add(gi)

    noise_info = []
    noise_set = set()
    for tok, gis in tail_groups.items():
        if len(gis) < NOISE_CHECK_MIN_GROUPS:
            continue
        cuisine_counter = Counter()
        for gi in gis:
            cuisine_counter.update(groups[gi]["cuisines"])
        top_c, top_n = (
            cuisine_counter.most_common(1)[0]
            if cuisine_counter else (None, 0)
        )
        share = top_n / len(gis)
        is_noise = (
            len(gis) >= NOISE_MIN_GROUPS
            and share < NOISE_MAX_TOP_CUISINE_SHARE
        )
        if is_noise:
            noise_set.add(tok)
        noise_info.append({
            "word": tok,
            "restaurants_ending_with_it": len(gis),
            "top_cuisine": top_c,
            "top_cuisine_share": round(share, 2),
            "treated_as_noise": is_noise,
        })

    noise_info.sort(
        key=lambda x: (-x["restaurants_ending_with_it"], x["word"])
    )
    noise_words_found = sum(1 for x in noise_info if x["treated_as_noise"])

    patterns_file = {
        "unique_names": len(all_names),
        "names_with_comma": names_with_comma,
        "names_without_comma": names_without_comma,
        "names_with_dash_separator": names_with_dash,
        "names_with_parentheses": names_with_paren,
        "names_with_digits": names_with_digits,
        "names_with_non_ascii_characters": names_non_ascii,
        "names_all_caps": names_all_caps,
        "names_with_double_spaces": names_double_space,
        "names_ending_with_punctuation": names_end_punct,
        "names_very_short": names_very_short,
        "names_differing_only_by_letter_case": names_case_variants,
        "comma_segments_distribution": {
            (f"{k}+" if k == 5 else str(k)): v
            for k, v in sorted(segments_dist.items())
        },
        "last_words_of_restaurant_part": noise_info[:300],
        "largest_groups_by_ids": big_groups,
    }

    # ====================================================
    # 3) تجمعات الكلمة الأولى
    # ====================================================
    first_tok_groups = defaultdict(set)
    for gi, g in enumerate(groups):
        for k in g["keys"]:
            t = tokens(k)
            if t and len(t[0]) >= MIN_FIRST_TOKEN_LEN:
                first_tok_groups[t[0]].add(gi)

    first_token_entries = []
    for tok, gis in first_tok_groups.items():
        if len(gis) < 2:
            continue
        ms = sorted(
            gis,
            key=lambda gi: (
                -len(groups[gi]["names"]), -len(groups[gi]["ids"]), gi
            ),
        )
        head = groups[ms[0]]
        sims = [
            jaccard(head["cuisines"], groups[gi]["cuisines"])
            for gi in ms[1:]
        ]
        first_token_entries.append({
            "first_word": tok,
            "restaurants_starting_with_it": len(ms),
            "generic_word": len(ms) > GENERIC_MIN_GROUPS,
            "largest": head["display"],
            "total_ids": sum(len(groups[gi]["ids"]) for gi in ms),
            "avg_cuisine_similarity_to_largest": round(
                sum(sims) / len(sims), 2
            ),
            "members": [brief(groups[gi]) for gi in ms[:MAX_MEMBERS_SAVED]],
            "_groups": ms,
        })

    first_token_entries.sort(
        key=lambda e: (
            e["generic_word"], -e["restaurants_starting_with_it"],
            e["first_word"],
        )
    )

    ft_generic = sum(1 for e in first_token_entries if e["generic_word"])
    ft_non_generic = [e for e in first_token_entries if not e["generic_word"]]
    ft_groups_involved = len(
        {gi for e in ft_non_generic for gi in e["_groups"]}
    )

    first_token_file = {
        "note": (
            "Every first word that starts more than one restaurant. "
            "generic_word = starts more than "
            f"{GENERIC_MIN_GROUPS} restaurants (probably not a brand)."
        ),
        "first_words_total": len(first_token_entries),
        "generic_first_words": ft_generic,
        "clusters": strip_internal(first_token_entries)[:MAX_FIRST_TOKEN_SAVED],
    }

    # ====================================================
    # 4) قاعدة البادئة
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
    # 5) دليل branchName
    # ====================================================
    bn_acc = defaultdict(lambda: {"bids": set(), "bks": Counter()})
    for gi, g in enumerate(groups):
        for bk, bset in g["branch_brands"].items():
            hi = key_to_group.get(bk)
            if hi is None or hi == gi:
                continue
            acc = bn_acc[(gi, hi)]
            acc["bids"] |= bset
            acc["bks"][bk] += len(bset)

    bn_entries = []
    for (gi, hi), acc in bn_acc.items():
        g, h = groups[gi], groups[hi]
        ev = len(acc["bids"])
        share = ev / max(1, len(g["branches"]))
        conf = (
            "high"
            if ev >= BN_HIGH_MIN_BRANCHES or share >= BN_HIGH_MIN_SHARE
            else "medium"
        )
        bk = acc["bks"].most_common(1)[0][0]
        bn_entries.append({
            "confidence": conf,
            "restaurant": g["display"],
            "would_merge_into": h["display"],
            "evidence_branches": ev,
            "share_of_restaurant_branches": round(share, 2),
            "branch_name_brand_matched": bk,
            "sample_branch_name": bk_example.get(bk),
            "sample_names": sorted(g["names"])[:3],
            "head_sample_names": sorted(h["names"])[:3],
            "cuisine_similarity": round(
                jaccard(g["cuisines"], h["cuisines"]), 2
            ),
            "shared_cities": len(g["cities"] & h["cities"]),
            "_k": gi,
            "_h": hi,
        })

    bn_entries.sort(
        key=lambda e: (
            CONF_RANK[e["confidence"]], -e["evidence_branches"],
            e["restaurant"].lower(),
        )
    )
    bn_by_conf = Counter(e["confidence"] for e in bn_entries)
    bn_high_pairs = [
        (e["_k"], e["_h"]) for e in bn_entries if e["confidence"] == "high"
    ]
    bn_med_pairs = [
        (e["_k"], e["_h"]) for e in bn_entries if e["confidence"] == "medium"
    ]

    # ====================================================
    # 6) قاعدة الفواصل (" - " والأقواس) + الزوايد
    # ====================================================
    alt_map = defaultdict(set)
    stripped_map = defaultdict(set)
    for gi, g in enumerate(groups):
        for n in g["names"]:
            ak = alt_brand_key(n)
            if not ak:
                continue
            alt_map[ak].add(gi)

            t = tokens(ak)
            while len(t) > 1 and t[-1] in noise_set:
                t.pop()
            if t:
                stripped_map[" ".join(t)].add(gi)

    separator_pairs = pairs_from_map(
        {k: v for k, v in alt_map.items() if len(v) > 1}
    )
    noise_pairs = pairs_from_map(
        {k: v for k, v in stripped_map.items() if len(v) > 1}
    )

    # ====================================================
    # 7) نفس النص + تشابه إملائي
    # ====================================================
    squash_map = defaultdict(set)
    for gi, g in enumerate(groups):
        for key in g["keys"]:
            s = squash(key)
            if s:
                squash_map[s].add(gi)

    squash_entries = []
    squash_high_pairs = []
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
                squash_high_pairs.append((gi, ordered[0]))

    squash_entries.sort(
        key=lambda e: (CONF_RANK[e["confidence"]], e["same_text_without_spaces"])
    )

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
    # 8) دمج ممكن يكون غلط
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
            "mixed_min_ids": MIXED_MIN_IDS,
            "mixed_max_avg_similarity": MIXED_MAX_AVG_JACCARD,
        },
        "merged_by_shared_id_with_dissimilar_names": susp_by_id,
        "groups_with_mixed_cuisines": susp_mixed,
    }

    # ====================================================
    # التجمعات الناتجة من كل قاعدة + المحاكاة
    # ====================================================
    rules = {
        "same_text_high": squash_high_pairs,
        "prefix_high": prefix_high_pairs,
        "prefix_medium": prefix_med_pairs,
        "branchname_high": bn_high_pairs,
        "branchname_medium": bn_med_pairs,
        "separator_rule": separator_pairs,
        "noise_words_rule": noise_pairs,
    }

    rule_descriptions = {
        "same_text_high": "same text without spaces/punctuation, similar cuisines",
        "prefix_high": "name starts with an established restaurant name (high confidence)",
        "prefix_medium": "same as above, medium confidence",
        "branchname_high": "branchName of its branches starts with another restaurant's name (high)",
        "branchname_medium": "same as above, medium confidence",
        "separator_rule": "treat ' - ' and '(' like a comma when taking the restaurant name",
        "noise_words_rule": "separator rule + remove trailing generic words (see last_words_of_restaurant_part)",
    }

    rule_reports = {}
    for rname, pairs in rules.items():
        rep = rule_report(pairs)
        rep["description"] = rule_descriptions[rname]
        rule_reports[rname] = rep

    simulation = {
        "restaurants_now": total_groups,
    }
    for rname, rep in rule_reports.items():
        simulation[f"after_{rname}"] = rep["restaurants_after_rule"]

    recommended = (
        squash_high_pairs + prefix_high_pairs + bn_high_pairs + separator_pairs
    )
    everything = (
        recommended + prefix_med_pairs + bn_med_pairs + noise_pairs
    )
    simulation["after_high_confidence_rules_together"] = count_after(
        recommended
    )
    simulation["after_all_rules_together"] = count_after(everything)

    # ====================================================
    # بحث بالاسم (اختياري)
    # ====================================================
    terms = [t.strip() for t in SEARCH_TERMS.split(",") if t.strip()]
    search_result = {}
    for t in terms:
        st = squash(t)
        matches = []
        if st:
            for g in groups:
                if any(st in squash(n) for n in g["names"]):
                    matches.append(g)
        matches.sort(key=lambda g: (-len(g["names"]), g["display"].lower()))
        search_result[t] = [
            {
                **brief(g),
                "keys": g["keys"],
                "all_names_without_comma": no_comma(g),
                "is_established": established(g),
                "names_list": sorted(g["names"])[:50],
            }
            for g in matches
        ]

    # ====================================================
    # summary
    # ====================================================
    summary = {
        "General info": {
            "files": len(files),
            "total_records": total_records,
        },
        "Name formats": {
            "unique_names": len(all_names),
            "names_with_comma": names_with_comma,
            "names_without_comma": names_without_comma,
            "names_with_dash_separator": names_with_dash,
            "names_with_parentheses": names_with_paren,
            "names_with_digits": names_with_digits,
            "names_with_non_ascii_characters": names_non_ascii,
            "names_all_caps": names_all_caps,
            "names_with_double_spaces": names_double_space,
            "names_ending_with_punctuation": names_end_punct,
            "names_very_short": names_very_short,
            "names_differing_only_by_letter_case": names_case_variants,
        },
        "Current grouping": {
            "restaurants_after_grouping": total_groups,
            "groups_merged_by_shared_id": groups_merged_by_id,
            "groups_with_single_name": groups_single_name,
            "groups_where_no_name_has_comma": groups_no_comma,
            "groups_established": groups_established,
        },
        "Generic words at the end of restaurant names": {
            "last_words_checked": len(noise_info),
            "treated_as_noise": noise_words_found,
        },
        "First-word clusters": {
            "first_words_starting_several_restaurants": len(first_token_entries),
            "generic_first_words": ft_generic,
            "non_generic_first_words": len(ft_non_generic),
            "restaurants_in_non_generic_clusters": ft_groups_involved,
        },
        "Prefix rule": {
            "candidates_total": len(prefix_entries),
            "high": prefix_by_conf.get("high", 0),
            "medium": prefix_by_conf.get("medium", 0),
            "low": prefix_by_conf.get("low", 0),
        },
        "branchName evidence": {
            "candidates_total": len(bn_entries),
            "high": bn_by_conf.get("high", 0),
            "medium": bn_by_conf.get("medium", 0),
        },
        "Same text / similar spelling": {
            "same_text_groups": len(squash_entries),
            "same_text_high": sum(
                1 for e in squash_entries if e["confidence"] == "high"
            ),
            "similar_spelling_pairs": len(similar_entries),
            "blocks_skipped_too_big": skipped_blocks,
        },
        "Possible wrong merges": {
            "merged_by_id_with_dissimilar_names": len(susp_by_id),
            "groups_with_mixed_cuisines": len(susp_mixed),
        },
        "Simulation: restaurants if a rule was applied": simulation,
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for section, values in summary.items():
        print(f"\n{section}")
        for k, v in values.items():
            print(f"{k:<46}: {v}")

    # ----------------------------------------------------
    # print-only info
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("PRINT-ONLY INFO (also saved in the summary file)")
    print("=" * 70)

    print("\nComma segments per name:")
    for k, v in sorted(segments_dist.items()):
        label = f"{k}+" if k == 5 else str(k)
        print(f"  {label} segment(s): {v} names")

    print_block(
        "Last words of the restaurant part "
        "(noise = many restaurants, mixed cuisines)",
        len(noise_info),
        [
            f"{x['word']}: {x['restaurants_ending_with_it']} restaurants | "
            f"top cuisine {x['top_cuisine']} {x['top_cuisine_share']} | "
            f"{'NOISE' if x['treated_as_noise'] else 'kept'}"
            for x in noise_info[:TOP_TOKENS]
        ],
        PATTERNS_FILE,
    )

    print_block(
        "Largest restaurants after grouping (by ids)",
        total_groups,
        [
            f"{b['restaurant']}: {b['names']} names | {b['ids']} ids | "
            f"{b['branches']} branches"
            for b in big_groups[:TOP_N_PRINT]
        ],
        PATTERNS_FILE,
    )

    # ----------------------------------------------------
    # SAVE FILES
    # ----------------------------------------------------
    save_json(SUMMARY_FILE, {
        "summary": summary,
        "thresholds": {
            "est_min_names": EST_MIN_NAMES,
            "est_min_branches": EST_MIN_BRANCHES,
            "min_head_len": MIN_HEAD_LEN,
            "high_min_jaccard": HIGH_MIN_JACCARD,
            "med_min_jaccard": MED_MIN_JACCARD,
            "high_max_tail_tokens": HIGH_MAX_TAIL_TOKENS,
            "squash_min_jaccard": SQUASH_MIN_JACCARD,
            "fuzzy_min_ratio": FUZZY_MIN_RATIO,
            "suspicious_max_ratio": SUSPICIOUS_MAX_RATIO,
            "mixed_min_ids": MIXED_MIN_IDS,
            "mixed_max_avg_jaccard": MIXED_MAX_AVG_JACCARD,
            "generic_min_groups": GENERIC_MIN_GROUPS,
            "noise_min_groups": NOISE_MIN_GROUPS,
            "noise_max_top_cuisine_share": NOISE_MAX_TOP_CUISINE_SHARE,
            "bn_high_min_branches": BN_HIGH_MIN_BRANCHES,
            "bn_high_min_share": BN_HIGH_MIN_SHARE,
        },
        "comma_segments_distribution": {
            (f"{k}+" if k == 5 else str(k)): v
            for k, v in sorted(segments_dist.items())
        },
        "last_words_of_restaurant_part": noise_info[:TOP_TOKENS],
        "simulation": simulation,
    })
    save_json(PATTERNS_FILE, patterns_file)
    save_json(FIRST_TOKEN_FILE, first_token_file)
    save_json(PREFIX_FILE, {
        "note": (
            "Restaurants whose first words equal the name of an "
            "established restaurant, so they may belong to it. high = "
            "name without comma, not established, similar cuisines, "
            "shared city. Review before merging."
        ),
        "counts_by_confidence": dict(prefix_by_conf),
        "candidates": strip_internal(prefix_entries),
    })
    save_json(BRANCHNAME_FILE, {
        "note": (
            "The branchName of this restaurant's branches starts with the "
            "name of another restaurant. This is a second, independent "
            "signal besides the name column."
        ),
        "counts_by_confidence": dict(bn_by_conf),
        "candidates": strip_internal(bn_entries),
    })
    save_json(SIMILAR_FILE, similar_file)
    save_json(SUSPICIOUS_FILE, suspicious_file)
    save_json(RULE_MERGES_FILE, {
        "note": (
            "For each rule: the clusters of restaurants that would be "
            "merged if the rule was applied. Nothing here is applied to "
            "market_insights."
        ),
        "simulation": simulation,
        "rules": rule_reports,
    })
    save_json(SEARCH_FILE, search_result)

    # ----------------------------------------------------
    # SAMPLES
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print(f"SAMPLES (first {SAMPLES} only, full details are in the files)")
    print("=" * 70)

    print_block(
        "First-word clusters (non-generic, biggest first)",
        len(ft_non_generic),
        [
            f"'{e['first_word']}': {e['restaurants_starting_with_it']} "
            f"restaurants | largest {e['largest']} | avg cuisine similarity "
            f"{e['avg_cuisine_similarity_to_largest']}"
            for e in ft_non_generic[:TOP_N_PRINT]
        ],
        FIRST_TOKEN_FILE,
    )

    def prefix_line(e):
        return (
            f"{e['names'][0]} -> {e['would_merge_into']} "
            f"({e['head_names']} names) | prefix '{e['matched_prefix']}' "
            f"tail '{e['tail']}' | cuisines {e['cuisine_similarity']} | "
            f"shared cities {e['shared_cities']}"
        )

    for conf, n in (("high", TOP_N_PRINT), ("medium", SAMPLES), ("low", SAMPLES)):
        subset = [e for e in prefix_entries if e["confidence"] == conf]
        print_block(
            f"Prefix rule, {conf} confidence",
            len(subset),
            [prefix_line(e) for e in subset[:n]],
            PREFIX_FILE,
        )

    for conf, n in (("high", TOP_N_PRINT), ("medium", SAMPLES)):
        subset = [e for e in bn_entries if e["confidence"] == conf]
        print_block(
            f"branchName evidence, {conf} confidence",
            len(subset),
            [
                f"{e['sample_names'][0]} -> {e['would_merge_into']} | "
                f"{e['evidence_branches']} branches | e.g. branchName "
                f"'{e['sample_branch_name']}'"
                for e in subset[:n]
            ],
            BRANCHNAME_FILE,
        )

    print_block(
        "Same text without spaces/punctuation",
        len(squash_entries),
        [
            f"[{e['confidence']}] "
            + " | ".join(
                f"{g['restaurant']} ({g['names']} names)"
                for g in e["groups"][:3]
            )
            for e in squash_entries[:SAMPLES]
        ],
        SIMILAR_FILE,
    )

    print_block(
        "Similar spelling pairs",
        len(similar_entries),
        [
            f"{e['restaurant_a']['restaurant']} ~ "
            f"{e['restaurant_b']['restaurant']} "
            f"(similarity {e['similarity']}, cuisines "
            f"{e['cuisine_similarity']})"
            for e in similar_entries[:SAMPLES]
        ],
        SIMILAR_FILE,
    )

    for rname in ("separator_rule", "noise_words_rule"):
        rep = rule_reports[rname]
        print_block(
            f"Rule '{rname}': clusters it would create "
            f"(removes {rep['restaurants_removed']} restaurants)",
            rep["clusters_count"],
            [
                f"{c['result_name']} <- "
                + " | ".join(m["restaurant"] for m in c["members"][1:4])
                + f" (cuisine similarity {c['min_cuisine_similarity_to_largest']})"
                for c in rep["clusters"][:SAMPLES]
            ],
            RULE_MERGES_FILE,
        )

    print_block(
        "Merged by shared id but names look different",
        len(susp_by_id),
        [
            f"{e['restaurant']}: {e['most_different_pair'][0]} <> "
            f"{e['most_different_pair'][1]} (similarity {e['similarity']})"
            for e in susp_by_id[:SAMPLES]
        ],
        SUSPICIOUS_FILE,
    )

    print_block(
        "Groups with mixed cuisines (maybe a generic name)",
        len(susp_mixed),
        [
            f"{e['restaurant']}: {e['ids']} ids | avg similarity "
            f"{e['avg_cuisine_similarity_between_ids']} | "
            + ", ".join(c["cuisine"] for c in e["top_cuisines"][:3])
            for e in susp_mixed[:SAMPLES]
        ],
        SUSPICIOUS_FILE,
    )

    for t, matches in search_result.items():
        print_block(
            f"Search '{t}': groups containing it",
            len(matches),
            [
                f"{m['restaurant']}: {m['names']} names | {m['ids']} ids | "
                f"{m['branches']} branches | e.g. "
                f"{short_list(m['sample_names'], 3)}"
                for m in matches[:SEARCH_PRINT_MAX]
            ],
            SEARCH_FILE,
        )

    # ----------------------------------------------------
    # SAVED FILES
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{SUMMARY_FILE:<44}: summary + thresholds + simulation")
    print(f"{PATTERNS_FILE:<44}: name formats, generic words, largest groups")
    print(f"{FIRST_TOKEN_FILE:<44}: restaurants sharing a first word")
    print(f"{PREFIX_FILE:<44}: possible missed groupings (prefix rule)")
    print(f"{BRANCHNAME_FILE:<44}: possible missed groupings (branchName)")
    print(f"{SIMILAR_FILE:<44}: same text / similar spelling")
    print(f"{SUSPICIOUS_FILE:<44}: possible wrong merges")
    print(f"{RULE_MERGES_FILE:<44}: clusters each rule would create")
    print(f"{SEARCH_FILE:<44}: optional name search")


if __name__ == "__main__":
    main()