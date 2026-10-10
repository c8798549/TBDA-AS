import os
import re
import json
import difflib
import unicodedata
import csv
from collections import defaultdict, Counter
from itertools import combinations

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)
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
GROUPED_EXPORT_FILE = "restaurants_grouped_export.csv"

# ---------------- thresholds ----------------
EST_MIN_NAMES = 2                
EST_MIN_BRANCHES = 3             
MIN_HEAD_LEN = 4                 
HIGH_MIN_JACCARD = 0.5           
MED_MIN_JACCARD = 0.3            
HIGH_MAX_TAIL_TOKENS = 6         
SQUASH_MIN_JACCARD = 0.3         
FUZZY_MIN_RATIO = 0.88           
MAX_BLOCK_SIZE = 600             
SUSPICIOUS_MAX_RATIO = 0.6       
MIXED_MIN_IDS = 5                
MIXED_MAX_AVG_JACCARD = 0.3      
MIXED_MAX_IDS_COMPARED = 40      

MIN_FIRST_TOKEN_LEN = 3          
GENERIC_MIN_GROUPS = 25          
NOISE_CHECK_MIN_GROUPS = 3       
NOISE_MIN_GROUPS = 8             
NOISE_MAX_TOP_CUISINE_SHARE = 0.5  
BN_HIGH_MIN_BRANCHES = 2         
BN_HIGH_MIN_SHARE = 0.5          

SAMPLES = 5                      
TOP_N_PRINT = 10
TOP_TOKENS = 25                  
BIG_GROUPS = 20
MAX_MEMBERS_SAVED = 25           
MAX_CLUSTERS_SAVED = 300         
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
    return normalize_text(first_segment(name))


def alt_first_part(name):
    return SEP_RE.split(name, maxsplit=1)[0].strip()


def alt_brand_key(name):
    return normalize_text(alt_first_part(name))


def tokens(text):
    text = unicodedata.normalize("NFKC", text)
    text = APOSTROPHES.sub("", text)
    return re.findall(r"[^\W_]+", text.lower())


def squash(text):
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
    
    # New containers for slug, branchUrl, and record tracking per ID
    id_to_slug = {}
    id_to_urls = defaultdict(set)
    id_to_record_names = defaultdict(set)

    id_branch_brands = defaultdict(lambda: defaultdict(set))
    bk_example = {}

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
            city = norm_code(r.get("shopCity"))
            slug = clean(r.get("restaurantSlug"))
            url = clean(r.get("branchUrl"))

            if name:
                names.add(name)
                if _id:
                    id_to_record_names[_id].add(name)
            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)
            if _id and slug:
                id_to_slug[_id] = slug
            if _id and url:
                id_to_urls[_id].add(url)
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

    # ----------------------------------------------------
    # تصدير ملف الـ CSV المطلوب بالمواصفات المحددة
    # ----------------------------------------------------
    print(f"\nGenerating grouped export file: {GROUPED_EXPORT_FILE}")
    with open(GROUPED_EXPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["group_name", "grouped_ids", "restaurant_name", "restaurantSlug", "branchUrl"])

        for g in sorted(groups, key=lambda x: x["display"].lower()):
            group_name = g["display"]
            sorted_ids = sorted(list(g["ids"]), key=sort_key)
            all_ids_str = ", ".join(sorted_ids)

            # جمع كل الأسماء المرتبطة بهذه الـ IDs داخل التجمع
            group_records = []
            for rid in sorted_ids:
                r_names = id_to_record_names.get(rid, {group_name})
                r_slug = id_to_slug.get(rid, "")
                r_urls = id_to_urls.get(rid, {""})
                for r_name in r_names:
                    for r_url in r_urls:
                        group_records.append((group_name, all_ids_str, r_name, r_slug, r_url))

            # منع التكرار في الصفوف
            seen_rows = set()
            for row in group_records:
                if row not in seen_rows:
                    seen_rows.add(row)
                    writer.writerow(row)

    print(f"Export completed successfully: {GROUPED_EXPORT_FILE}")

    # بقية الكود التلخيصي والإحصائي كما هو ...
    all_names = sorted(names)
    names_with_comma = sum(1 for n in all_names if "," in n)
    names_without_comma = len(all_names) - names_with_comma
    names_with_dash = sum(1 for n in all_names if " - " in n)
    names_with_paren = sum(1 for n in all_names if "(" in n or ")" in n)
    names_with_digits = sum(1 for n in all_names if any(ch.isdigit() for ch in n))
    names_non_ascii = sum(1 for n in all_names if re.search(r"[^\x00-\x7F]", n))
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
    groups_no_comma = sum(1 for g in groups if all("," not in n for n in g["names"]))
    groups_established = sum(1 for g in groups if (len(g["names"]) >= EST_MIN_NAMES or len(g["branches"]) >= EST_MIN_BRANCHES))

    summary = {
        "General info": {"files": len(files), "total_records": total_records},
        "Current grouping": {"restaurants_after_grouping": total_groups}
    }
    save_json(SUMMARY_FILE, {"summary": summary})
    print(f"\nAll audits and export file '{GROUPED_EXPORT_FILE}' are ready!")

if __name__ == "__main__":
    main()