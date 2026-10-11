import os
import re
import json
import unicodedata
from collections import defaultdict, Counter

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

# ---------------- grouping decisions (from the manual review) ----------------
# ملف المراجعة اللي فيه عمود validation: T = دمج، F = ميتدمجش، فاضي = من غير تغيير
DECISIONS_FILE = os.environ.get("GROUPING_DECISIONS", "restaurant_grouping_review.json")

# false = كل النتايج على التجميع القديم (لحد ما المدير يوافق)
# true  = كل النتايج على التجميع المراجَع
USE_REVIEWED_GROUPING = os.environ.get(
    "USE_REVIEWED_GROUPING", "false"
).strip().lower() in ("1", "true", "yes")

# true = الـ F في مجموعة حالية (متجمعة بنفس الاسم) بتفصل الـ ids
APPLY_SPLITS = True

# أسباب بتدل إن المجموعة اقتراح جديد (مش تجميع حالي)
PROPOSAL_RULES = ("prefix", "branchname", "noise", "separator", "same_text")

# ---------------- output files ----------------
SUMMARY_FILE = "market_insights.json"
RESTAURANTS_FILE = "unique_restaurants.json"
RESTAURANTS_GROUPED_FILE = "restaurants_grouped.json"
BRANCHES_FILE = "unique_branches.json"
BRANCHES_GROUPED_FILE = "branches_grouped.json"
DUPLICATES_FILE = "duplicates_details.json"
MULTI_NAMES_FILE = "restaurants_multiple_names.json"
MERGED_BY_ID_FILE = "restaurants_merged_by_id.json"
BRANCH_COUNTS_FILE = "restaurant_branch_counts.json"
MULTI_BRANCHES_FILE = "restaurants_multiple_branches.json"
CUISINE_FILE = "cuisine_distribution.json"
CITY_FILE = "restaurants_by_city.json"
AREA_BRANCHES_FILE = "branches_by_area.json"
DENSITY_FILE = "restaurant_density_by_area.json"
COMPARISON_FILE = "grouping_comparison.json"

SAMPLES = 5                  # عدد الأمثلة اللي بتتطبع في اللوج
CUISINE_SAMPLES = 10         # عدد الـ cuisines اللي بتتطبع في اللوج
AREA_SAMPLES = 10            # عدد المدن/المناطق اللي بتتطبع في اللوج
MULTI_LOCATION_PRINT = 20    # أقصى عدد فروع بتتطبع في الحالات الغريبة
COMPARISON_PRINT = 10        # عدد حالات المقارنة اللي بتتطبع

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
    """يوحّد أكواد المدينة/المنطقة: 1 و 1.0 و "1" كلهم "1"."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:          # NaN
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text or None


def sort_key(value):
    """ترتيب الـ ids رقميًا لو أرقام، وإلا نصيًا."""
    value = str(value)
    if value.isdigit():
        return (0, int(value), value)
    return (1, 0, value)


def first_segment(name):
    """الجزء الأول من الاسم قبل أول فاصلة (دا هو اسم المطعم)."""
    return name.split(",")[0].strip()


def brand_key(name):
    """
    مفتاح المقارنة:
    - الجزء الأول قبل أول فاصلة
    - توحيد unicode
    - حذف كل أنواع الـ apostrophe
    - lowercase + توحيد المسافات
    """
    seg = unicodedata.normalize("NFKC", first_segment(name))
    seg = APOSTROPHES.sub("", seg)
    seg = re.sub(r"\s+", " ", seg).strip().lower()
    return seg


def top_name(counter):
    """أكتر قيمة اتكررت (None لو مفيش)."""
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def split_counts(counts):
    """يقسم المطاعم حسب عدد الفروع."""
    return {
        "multiple_branches": sum(1 for c in counts if c > 1),
        "single_branch": sum(1 for c in counts if c == 1),
        "no_branch_id": sum(1 for c in counts if c == 0),
    }


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


def print_block(title, total, lines, filename):
    """عنوان + عدد + samples + مكان الملف الكامل."""
    print(f"\n{title} ({total})")

    for line in lines:
        print(f"  {line}")

    if total > len(lines):
        print(f"  ... and {total - len(lines)} more")

    print(f"  -> full details: {filename}")


def location_line(bid, info):
    """سطر لفرع ظهر بأكتر من قيمة (مدينة أو منطقة)."""
    return (
        f"{bid} | {info['branchName']} | "
        f"restaurants: {short_list(info['restaurants'], 3)} | "
        f"values {info['values']} -> counted in ALL: {info['counted_in']}"
    )


def load_decisions(path):
    """
    يقرا ملف المراجعة:
      T = الـ ids دي تتدمج في مطعم واحد
      F = ميتدمجش (ولو المجموعة حالية من غير اقتراح، الـ ids بتتفصل)
      فاضي = من غير تغيير
    بيرجع: (ids تتفصل، قايمة مجموعات ids تتدمج، الملف موجود؟)
    """
    if not os.path.exists(path):
        print(f"No decisions file at {path}: reviewed grouping = old grouping")
        return set(), [], False

    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if isinstance(rows, dict):
        rows = rows.get("records", [])

    labels = defaultdict(list)
    for r in rows:
        labels[r.get("Grouping label")].append(r)

    merges, separate = [], set()
    for _label, rs in labels.items():
        has_proposal = any(
            p in str(r.get("grouping_reason")) for r in rs for p in PROPOSAL_RULES
        )
        yes = [
            str(r["id"]) for r in rs
            if (r.get("validation") or "").strip().upper() == "T"
        ]
        no = [
            str(r["id"]) for r in rs
            if (r.get("validation") or "").strip().upper() == "F"
        ]

        if len(yes) > 1:
            merges.append(yes)

        # F في مجموعة حالية (من غير اقتراح): نفصل الـ ids
        if APPLY_SPLITS and no and not has_proposal and len(rs) > 1:
            separate.update(no)

    return separate, merges, True


def logo_link(logo_sets):
    """هل المجموعات دي مربوطة ببعض بلوجو مشترك؟"""
    if not any(logo_sets):
        return "no_logo_data", None

    n = len(logo_sets)
    p = list(range(n))

    def f(x):
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if logo_sets[i] & logo_sets[j]:
                ri, rj = f(i), f(j)
                if ri != rj:
                    p[rj] = ri

    comps = len({f(i) for i in range(n)})
    return ("linked_by_logo" if comps == 1 else "not_linked_by_logo"), comps


def logo_disjoint(logo_sets):
    """هل الأجزاء اللي اتفصلت ليها لوجوهات مختلفة؟"""
    if not all(logo_sets):
        return "no_logo_data"

    for i in range(len(logo_sets)):
        for j in range(i + 1, len(logo_sets)):
            if logo_sets[i] & logo_sets[j]:
                return "share_a_logo"
    return "different_logos"


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
    # restaurants (id / name)
    ids, names, names_lower = set(), set(), set()
    id_to_names = defaultdict(Counter)
    name_to_ids = defaultdict(set)

    # branches (branchId / branchName)
    branch_ids, branch_names, branch_names_lower = set(), set(), set()
    branch_id_to_names = defaultdict(Counter)
    branch_name_to_ids = defaultdict(set)

    # (restaurant id, branchId) -> Counter(branchName)
    pair_names = defaultdict(Counter)

    # cuisines: id -> set of cuisines (lowercase key) + شكل الكتابة
    id_to_cuisines = defaultdict(set)
    cuisine_forms = defaultdict(Counter)

    # موقع الفرع: branchId -> Counter(shopCity / shopArea)
    branch_city = defaultdict(Counter)
    branch_area = defaultdict(Counter)

    # (branchId, area, city) اللي ظهروا مع بعض في نفس السجل
    branch_loc_triples = set()

    # لوجو المطعم (دليل للمقارنة بين التجميعين)
    id_to_logos = defaultdict(set)

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
            area = norm_code(r.get("shopArea"))
            logo = clean(r.get("logo"))

            # restaurants
            if _id:
                ids.add(_id)
            if name:
                names.add(name)
                names_lower.add(name.lower())
            if _id and name:
                id_to_names[_id][name] += 1
                name_to_ids[name].add(_id)
            if _id and logo:
                id_to_logos[_id].add(logo)

            # branches
            if b_id:
                branch_ids.add(b_id)
            if b_name:
                branch_names.add(b_name)
                branch_names_lower.add(b_name.lower())
            if b_id and b_name:
                branch_id_to_names[b_id][b_name] += 1
                branch_name_to_ids[b_name].add(b_id)

            # restaurant -> branch
            if _id and b_id:
                counter = pair_names[(_id, b_id)]
                if b_name:
                    counter[b_name] += 1

            # cuisines (نقسم على الفاصلة)
            if _id and cuisine_str:
                for part in cuisine_str.split(","):
                    p = re.sub(r"\s+", " ", part).strip()
                    if not p:
                        continue
                    ck = p.lower()
                    id_to_cuisines[_id].add(ck)
                    cuisine_forms[ck][p] += 1

            # location (مدينة / منطقة المحل)
            if b_id and city:
                branch_city[b_id][city] += 1
            if b_id and area:
                branch_area[b_id][area] += 1
            if b_id and city and area:
                branch_loc_triples.add((b_id, area, city))

        print(f"[{i}/{len(files)}] {key} -> {len(records)} records")

    # restaurant id -> its branchIds
    id_to_branch_ids = defaultdict(set)
    for (rid, bid) in pair_names:
        id_to_branch_ids[rid].add(bid)

    # ----------------------------------------------------
    # multiple mappings
    # ----------------------------------------------------
    multi_name_ids = {
        k: sorted(v.keys())
        for k, v in sorted(id_to_names.items(), key=lambda x: sort_key(x[0]))
        if len(v) > 1
    }
    multi_id_names = {
        k: sorted(v, key=sort_key)
        for k, v in sorted(name_to_ids.items())
        if len(v) > 1
    }
    multi_name_branch_ids = {
        k: sorted(v.keys())
        for k, v in sorted(
            branch_id_to_names.items(), key=lambda x: sort_key(x[0])
        )
        if len(v) > 1
    }
    multi_id_branch_names = {
        k: sorted(v, key=sort_key)
        for k, v in sorted(branch_name_to_ids.items())
        if len(v) > 1
    }

    # ----------------------------------------------------
    # unique restaurants / branches  (واحد لكل id)
    # ----------------------------------------------------
    unique_restaurants = [
        {"id": rid, "name": top_name(id_to_names[rid])}
        for rid in sorted(id_to_names.keys(), key=sort_key)
    ]

    unique_branches = [
        {"branchId": bid, "branchName": top_name(branch_id_to_names[bid])}
        for bid in sorted(branch_id_to_names.keys(), key=sort_key)
    ]

    # ----------------------------------------------------
    # group restaurants: التجميع القديم + التجميع المراجَع
    #   القاعدة 1: الجزء الأول من الاسم (قبل أول فاصلة)
    #   القاعدة 2: أي أسماء ظهرت مع نفس الـ id تتدمج (id = المطعم)
    #   المراجَع: + قرارات ملف المراجعة (T تتدمج، F بتتفصل)
    # ----------------------------------------------------
    def build_components(separate, merges):
        def key_for(name, rid):
            k = brand_key(name)
            if k and rid in separate:
                return f"{k}#{rid}"       # الـ id دا مطعم لوحده
            return k

        b_names = defaultdict(set)
        b_ids = defaultdict(set)
        b_variants = defaultdict(Counter)
        counted = set()

        for name in sorted(names):        # sorted عشان النتيجة تبقى ثابتة
            for rid in sorted(name_to_ids[name], key=sort_key):
                k = key_for(name, rid)
                if not k:
                    continue
                b_names[k].add(name)
                b_ids[k].add(rid)
                if (k, name) not in counted:
                    counted.add((k, name))
                    b_variants[k][first_segment(name)] += 1

        parent = {k: k for k in b_names}

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

        # نفس الـ id بأسماء مختلفة يتدمج
        for rid, counter in id_to_names.items():
            ks = [key_for(n, rid) for n in counter]
            ks = [k for k in ks if k]
            for other in ks[1:]:
                union(ks[0], other)

        # قرارات المراجعة: الـ ids اللي T تتدمج
        not_found = 0
        for ids_list in merges:
            anchor = None
            for rid in ids_list:
                ks = [key_for(n, rid) for n in id_to_names.get(rid, ())]
                ks = [k for k in ks if k in parent]
                if not ks:
                    not_found += 1
                    continue
                if anchor is None:
                    anchor = ks[0]
                for k in ks:
                    union(anchor, k)

        comps = defaultdict(list)
        for k in b_names:
            comps[find(k)].append(k)

        return {
            "brand_to_names": b_names,
            "brand_to_ids": b_ids,
            "brand_variants": b_variants,
            "components": comps,
            "not_found": not_found,
        }

    separate_ids, manual_merges, decisions_found = load_decisions(DECISIONS_FILE)

    old_g = build_components(set(), [])
    new_g = build_components(separate_ids, manual_merges)

    if USE_REVIEWED_GROUPING and not decisions_found:
        print("WARNING: USE_REVIEWED_GROUPING is on but no decisions file was "
              "found, so the old grouping is used.")

    active = new_g if USE_REVIEWED_GROUPING else old_g
    grouping_used = "reviewed" if USE_REVIEWED_GROUPING else "old"
    print(f"Grouping used for all outputs: {grouping_used} "
          f"(old = {len(old_g['components'])} restaurants, "
          f"reviewed = {len(new_g['components'])} restaurants)")

    brand_to_names = active["brand_to_names"]
    brand_to_ids = active["brand_to_ids"]
    brand_variants = active["brand_variants"]
    components = active["components"]

    # ----------------------------------------------------
    # مقارنة التجميع القديم بالمراجَع (للمدير)
    # ----------------------------------------------------
    def comp_display(g, root):
        variants = Counter()
        for k in g["components"][root]:
            variants.update(g["brand_variants"][k])
        return sorted(
            variants.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

    def id_root_map(g):
        m = {}
        for root, ks in g["components"].items():
            for k in ks:
                for rid in g["brand_to_ids"][k]:
                    m[rid] = root
        return m

    def logos_of(rids):
        s = set()
        for rid in rids:
            s |= id_to_logos.get(rid, set())
        return s

    old_root = id_root_map(old_g)
    new_root = id_root_map(new_g)

    new_members = defaultdict(set)
    for rid, nr in new_root.items():
        new_members[nr].add(rid)

    old_members = defaultdict(set)
    for rid, orr in old_root.items():
        old_members[orr].add(rid)

    merged_entries = []
    for nr in sorted(new_members):
        rids = new_members[nr]
        olds = sorted({old_root[r] for r in rids if r in old_root})
        if len(olds) > 1:
            logo_sets = [logos_of(old_members[o]) for o in olds]
            evidence, comps_n = logo_link(logo_sets)
            merged_entries.append({
                "restaurant": comp_display(new_g, nr),
                "old_groups_merged": len(olds),
                "ids": len(rids),
                "old_groups": [
                    {
                        "restaurant": comp_display(old_g, o),
                        "ids": sorted(old_members[o], key=sort_key),
                    }
                    for o in olds
                ],
                "logo_evidence": evidence,
                "logo_components": comps_n,
            })

    merged_entries.sort(
        key=lambda e: (-e["old_groups_merged"], e["restaurant"].lower())
    )

    separated_entries = []
    for orr in sorted(old_members):
        rids = old_members[orr]
        news = sorted({new_root[r] for r in rids if r in new_root})
        if len(news) > 1:
            parts = [
                sorted(
                    (r for r in rids if new_root.get(r) == nr), key=sort_key
                )
                for nr in news
            ]
            separated_entries.append({
                "restaurant": comp_display(old_g, orr),
                "ids": sorted(rids, key=sort_key),
                "split_into": [
                    {"restaurant": comp_display(new_g, nr), "ids": part}
                    for nr, part in zip(news, parts)
                ],
                "logo_evidence": logo_disjoint(
                    [logos_of(part) for part in parts]
                ),
            })

    separated_entries.sort(key=lambda e: e["restaurant"].lower())

    merges_removed = sum(e["old_groups_merged"] - 1 for e in merged_entries)
    splits_added = sum(len(e["split_into"]) - 1 for e in separated_entries)

    logo_merge_counts = Counter(e["logo_evidence"] for e in merged_entries)
    logo_split_counts = Counter(e["logo_evidence"] for e in separated_entries)

    comparison_file = {
        "note": (
            "Differences between the old grouping and the reviewed grouping "
            "(built from the manual review file). logo_evidence is an extra "
            "hint only: linked_by_logo / different_logos support the "
            "decision; not_linked_by_logo / share_a_logo contradict it; "
            "different URLs of the same logo can look different."
        ),
        "grouping_used_for_all_outputs": grouping_used,
        "restaurants_old_grouping": len(old_g["components"]),
        "restaurants_reviewed_grouping": len(new_g["components"]),
        "merged_by_review": merged_entries,
        "separated_by_review": separated_entries,
    }

    # ----------------------------------------------------
    # build restaurants (the active grouping)
    # ----------------------------------------------------
    grouped = {}            # restaurants after grouping
    branches_grouped = {}   # branches per restaurant after grouping
    rep_ids = {}            # restaurant (display) -> representative id
    brand_cuisines = {}     # restaurant (display) -> set of cuisines
    brand_cities = {}       # restaurant (display) -> set of cities
    brand_areas = {}        # restaurant (display) -> set of areas
    branch_to_brands = defaultdict(set)   # branchId -> المطاعم اللي ظهر فيها
    used_rep_ids = set()

    # الفروع الفريدة بعد التجميع (set بتشيل التكرار)
    grouped_branch_ids = set()
    grouped_branch_names = set()
    grouped_branch_names_lower = set()

    for root in sorted(components):
        keys = sorted(components[root])

        brand_names = set()
        comp_ids = set()
        variants = Counter()
        for k in keys:
            brand_names |= brand_to_names[k]
            comp_ids |= brand_to_ids[k]
            variants.update(brand_variants[k])

        # id تمثيلي للمطعم = أقل id من ids المطعم
        candidates = sorted(comp_ids, key=sort_key)
        rep_id = next(
            (c for c in candidates if c not in used_rep_ids), None
        )
        if rep_id is None:
            rep_id = candidates[0] if candidates else f"no_id:{root}"
        used_rep_ids.add(rep_id)

        # اسم العرض: الأكتر تكرارًا، ولو تعادل الأقصر
        display = sorted(
            variants.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

        if display in grouped:
            display = f"{display} (id {rep_id})"
        rep_ids[display] = rep_id

        # فروع المطعم: branchId -> Counter(branchName)
        brand_branches = {}
        cuisines = set()
        for rid in comp_ids:
            for bid in id_to_branch_ids.get(rid, ()):
                brand_branches.setdefault(bid, Counter()).update(
                    pair_names[(rid, bid)]
                )
            cuisines.update(id_to_cuisines.get(rid, ()))

        brand_cuisines[display] = cuisines

        # كل مدن ومناطق فروع المطعم (set، فكل مدينة/منطقة بتتعدّ مرة)
        cities_set = set()
        areas_set = set()
        for bid in brand_branches:
            cities_set.update(branch_city.get(bid, ()))
            areas_set.update(branch_area.get(bid, ()))
        brand_cities[display] = cities_set
        brand_areas[display] = areas_set

        # نسجل كل فرع ظهر تحت أنهي مطعم
        for bid in brand_branches:
            branch_to_brands[bid].add(display)

        b_names = set()
        for c in brand_branches.values():
            b_names.update(c.keys())
        b_names_lower = {n.lower() for n in b_names}

        grouped_branch_ids.update(brand_branches)
        grouped_branch_names.update(b_names)
        grouped_branch_names_lower.update(b_names_lower)

        grouped[display] = {
            "unique_names": len(brand_names),
            "unique_ids": len(comp_ids),
            "unique_branch_ids": len(brand_branches),
            "merged_by_shared_id": len(keys) > 1,
            "name_variants": sorted(variants.keys()),
            "ids": sorted(comp_ids, key=sort_key),
            "names": sorted(brand_names),
        }

        branches_grouped[display] = {
            "unique_branch_ids": len(brand_branches),
            "unique_branch_names": len(b_names),
            "unique_branch_names_case_insensitive": len(b_names_lower),
            "branches": [
                {
                    "branchId": bid,
                    "branchName": top_name(brand_branches[bid]),
                }
                for bid in sorted(brand_branches, key=sort_key)
            ],
        }

    # المطاعم اللي اتجمع فيها أكتر من اسم
    merged = {b: g for b, g in grouped.items() if g["unique_names"] > 1}

    # المطاعم اللي اتدمجت لأن فيها أكتر من مفتاح (نفس الـ id أو قرار مراجعة)
    merged_by_id = {
        b: {
            "unique_ids": g["unique_ids"],
            "unique_branch_ids": g["unique_branch_ids"],
            "name_variants": g["name_variants"],
            "ids": g["ids"],
            "names": g["names"],
        }
        for b, g in grouped.items()
        if g["merged_by_shared_id"]
    }

    # ----------------------------------------------------
    # branch ids shared between more than one restaurant
    # ----------------------------------------------------
    shared_branch_ids = {
        bid: sorted(brands)
        for bid, brands in sorted(
            branch_to_brands.items(), key=lambda x: sort_key(x[0])
        )
        if len(brands) > 1
    }

    # ----------------------------------------------------
    # فروع ظهرت بأكتر من مدينة / منطقة (بتتعدّ في كلهم)
    # ----------------------------------------------------
    def multi_location_details(branch_counters):
        details = {}
        for bid in sorted(
            (b for b, c in branch_counters.items() if len(c) > 1),
            key=sort_key,
        ):
            details[bid] = {
                "branchName": top_name(branch_id_to_names.get(bid)),
                "restaurants": sorted(branch_to_brands.get(bid, ())),
                "values": dict(branch_counters[bid].most_common()),
                "counted_in": sorted(branch_counters[bid], key=sort_key),
            }
        return details

    branches_multi_city_details = multi_location_details(branch_city)
    branches_multi_area_details = multi_location_details(branch_area)

    # ----------------------------------------------------
    # branches per restaurant (after grouping)
    # ----------------------------------------------------
    restaurant_branch_counts = {}        # كل المطاعم
    restaurants_multiple_branches = {}   # اللي ليها أكتر من فرع

    ordered = sorted(
        grouped.items(),
        key=lambda x: (-x[1]["unique_branch_ids"], x[0]),
    )

    for display, g in ordered:
        rid = rep_ids[display]

        restaurant_branch_counts[rid] = {
            "restaurant": display,
            "num_branches": g["unique_branch_ids"],
            "num_ids": g["unique_ids"],
        }

        if g["unique_branch_ids"] > 1:
            restaurants_multiple_branches[rid] = {
                "restaurant": display,
                "num_branches": g["unique_branch_ids"],
                "ids": g["ids"],
                "branches": branches_grouped[display]["branches"],
            }

    # ----------------------------------------------------
    # multiple branches vs single branch
    # ----------------------------------------------------
    by_id_counts = [len(id_to_branch_ids.get(rid, ())) for rid in ids]
    by_group_counts = [g["unique_branch_ids"] for g in grouped.values()]

    by_id_split = split_counts(by_id_counts)
    by_group_split = split_counts(by_group_counts)

    branches_distribution = {
        str(k): v for k, v in sorted(Counter(by_group_counts).items())
    }

    # ----------------------------------------------------
    # average / ratio (after grouping)
    # ----------------------------------------------------
    total_restaurants = len(grouped)
    total_branches = len(grouped_branch_ids)

    avg_branches = safe_div(total_branches, total_restaurants)
    restaurants_pct_of_branches = safe_div(
        total_restaurants * 100, total_branches
    )
    multi_pct = safe_div(
        by_group_split["multiple_branches"] * 100, total_restaurants
    )
    single_pct = safe_div(
        by_group_split["single_branch"] * 100, total_restaurants
    )

    # ----------------------------------------------------
    # cuisine distribution (after grouping)
    # ----------------------------------------------------
    cuisine_counter = Counter()
    for cuisines in brand_cuisines.values():
        for c in cuisines:
            cuisine_counter[c] += 1

    cuisines_per_restaurant = Counter(
        len(c) for c in brand_cuisines.values()
    )

    with_cuisine = sum(1 for c in brand_cuisines.values() if c)
    without_cuisine = total_restaurants - with_cuisine

    restaurants_per_cuisine = [
        {
            "cuisine": top_name(cuisine_forms[c]),
            "restaurants": n,
            "percentage_of_restaurants": safe_div(
                n * 100, total_restaurants
            ),
        }
        for c, n in sorted(
            cuisine_counter.items(), key=lambda x: (-x[1], x[0])
        )
    ]

    cuisine_distribution = {
        "total_restaurants": total_restaurants,
        "restaurants_with_cuisine": with_cuisine,
        "restaurants_without_cuisine": without_cuisine,
        "unique_cuisines": len(cuisine_counter),
        "note": (
            "A restaurant can have several cuisines, so it is counted under "
            "each one. The percentages can add up to more than 100."
        ),
        "restaurants_per_cuisine": restaurants_per_cuisine,
        "cuisines_per_restaurant_distribution": {
            str(k): v for k, v in sorted(cuisines_per_restaurant.items())
        },
    }

    # ----------------------------------------------------
    # area -> city  (من الثلاثية: فرع + منطقة + مدينة)
    # ----------------------------------------------------
    area_to_cities = defaultdict(Counter)
    for (bid, a, c) in branch_loc_triples:
        area_to_cities[a][c] += 1

    def area_city(area):
        return top_name(area_to_cities.get(area))

    # مناطق ظهرت تحت أكتر من مدينة (المفروض صفر)
    areas_multi_city = {
        a: dict(c.most_common())
        for a, c in sorted(
            area_to_cities.items(), key=lambda x: sort_key(x[0])
        )
        if len(c) > 1
    }

    # عدد المناطق في كل مدينة
    city_areas = Counter(area_city(a) for a in area_to_cities)

    # ----------------------------------------------------
    # city distribution (restaurants after grouping + branches)
    # ----------------------------------------------------
    all_branches_count = len(branch_ids)

    city_restaurants = Counter()
    for cities in brand_cities.values():
        for c in cities:
            city_restaurants[c] += 1

    # الفرع بيتعدّ مرة في كل مدينة ظهر فيها
    city_branches = Counter()
    for bid in branch_ids:
        for c in branch_city.get(bid, ()):
            city_branches[c] += 1

    all_cities = set(city_restaurants) | set(city_branches)

    restaurants_per_city = [
        {
            "city": c,
            "restaurants": city_restaurants.get(c, 0),
            "pct_of_restaurants": safe_div(
                city_restaurants.get(c, 0) * 100, total_restaurants
            ),
            "branches": city_branches.get(c, 0),
            "pct_of_branches": safe_div(
                city_branches.get(c, 0) * 100, all_branches_count
            ),
            "areas": city_areas.get(c, 0),
        }
        for c in sorted(
            all_cities,
            key=lambda c: (-city_restaurants.get(c, 0), sort_key(c)),
        )
    ]

    cities_per_restaurant = Counter(len(c) for c in brand_cities.values())
    restaurants_with_city = sum(1 for c in brand_cities.values() if c)
    restaurants_without_city = total_restaurants - restaurants_with_city
    branches_with_city = sum(1 for bid in branch_ids if bid in branch_city)
    branch_city_links = sum(city_branches.values())

    city_distribution = {
        "total_restaurants": total_restaurants,
        "restaurants_with_city": restaurants_with_city,
        "restaurants_without_city": restaurants_without_city,
        "unique_cities": len(all_cities),
        "branch_city_links": branch_city_links,
        "note": (
            "A restaurant is counted once under each city where it has a "
            "branch. A branch is counted once under each city it appears "
            "in (not once per record), so a branch seen in two cities is "
            "counted in both and the totals can be slightly above the "
            "number of branches. City values are codes (no names in the "
            "data)."
        ),
        "restaurants_per_city": restaurants_per_city,
        "cities_per_restaurant_distribution": {
            str(k): v for k, v in sorted(cities_per_restaurant.items())
        },
    }

    # ----------------------------------------------------
    # area distribution: branches per area
    # ----------------------------------------------------
    # الفرع بيتعدّ مرة في كل منطقة ظهر فيها
    area_branches = Counter()
    for bid in branch_ids:
        for a in branch_area.get(bid, ()):
            area_branches[a] += 1

    branches_with_area = sum(1 for bid in branch_ids if bid in branch_area)
    branch_area_links = sum(area_branches.values())

    branches_per_area = [
        {
            "area": a,
            "city": area_city(a),
            "branches": n,
            "pct_of_branches": safe_div(n * 100, all_branches_count),
        }
        for a, n in sorted(
            area_branches.items(), key=lambda x: (-x[1], sort_key(x[0]))
        )
    ]

    area_branches_distribution = {
        "total_branches": all_branches_count,
        "branches_with_area": branches_with_area,
        "branches_without_area": all_branches_count - branches_with_area,
        "unique_areas": len(area_branches),
        "branch_area_links": branch_area_links,
        "branches_with_multiple_areas": len(branches_multi_area_details),
        "areas_with_multiple_cities": areas_multi_city,
        "note": (
            "A branch is counted once under each shopArea it appears in "
            "(by branchId, not by record). A branch seen in two areas is "
            "counted in both, so the total can be slightly above the "
            "number of branches. Area values are codes (no names in the "
            "data)."
        ),
        "branches_per_area": branches_per_area,
    }

    # ----------------------------------------------------
    # density: restaurants per area (after grouping)
    # ----------------------------------------------------
    area_restaurants = Counter()
    for areas in brand_areas.values():
        for a in areas:
            area_restaurants[a] += 1

    all_areas = set(area_restaurants) | set(area_branches)

    density_list = [
        {
            "area": a,
            "city": area_city(a),
            "restaurants": area_restaurants.get(a, 0),
            "pct_of_restaurants": safe_div(
                area_restaurants.get(a, 0) * 100, total_restaurants
            ),
            "branches": area_branches.get(a, 0),
            "avg_branches_per_restaurant": safe_div(
                area_branches.get(a, 0), area_restaurants.get(a, 0)
            ),
        }
        for a in sorted(
            all_areas,
            key=lambda a: (-area_restaurants.get(a, 0), sort_key(a)),
        )
    ]

    top_areas = density_list[:AREA_SAMPLES]
    bottom_areas = sorted(
        density_list, key=lambda x: (x["restaurants"], sort_key(x["area"]))
    )[:AREA_SAMPLES]

    areas_with_single_restaurant = sum(
        1 for a in density_list if a["restaurants"] == 1
    )
    restaurants_with_area = sum(1 for a in brand_areas.values() if a)

    density_distribution = {
        "total_restaurants": total_restaurants,
        "restaurants_with_area": restaurants_with_area,
        "restaurants_without_area": total_restaurants - restaurants_with_area,
        "unique_areas": len(all_areas),
        "areas_with_single_restaurant": areas_with_single_restaurant,
        "note": (
            "Density here = number of restaurants (after grouping) that have "
            "at least one branch in the area; a restaurant is counted once "
            "per area even if it has several branches there, and in every "
            "area where it has a branch. There is no area size or "
            "population in the data, so this is a count, not per km2."
        ),
        "highest_density_areas": top_areas,
        "lowest_density_areas": bottom_areas,
        "all_areas": density_list,
    }

    # ----------------------------------------------------
    # summary
    # ----------------------------------------------------
    summary = {
        "General info": {
            "files": len(files),
            "total_records": total_records,
        },
        "Number of restaurants": {
            "unique_id": len(ids),
            "unique_name": len(names),
            "unique_name_case_insensitive": len(names_lower),
        },
        "Number of restaurants after grouping": {
            "grouping_used_for_all_outputs": grouping_used,
            "unique_restaurants_after_grouping": len(grouped),
            "restaurants_with_multiple_names": len(merged),
            "restaurants_with_several_name_keys": len(merged_by_id),
        },
        "Grouping: old vs reviewed": {
            "decisions_file_found": decisions_found,
            "restaurants_old_grouping": len(old_g["components"]),
            "restaurants_reviewed_grouping": len(new_g["components"]),
            "difference_reviewed_minus_old": (
                len(new_g["components"]) - len(old_g["components"])
            ),
            "old_groups_merged_by_review": merges_removed,
            "extra_restaurants_from_splits": splits_added,
            "review_merge_decisions": len(manual_merges),
            "review_separated_ids": len(separate_ids),
            "review_ids_not_found_in_data": new_g["not_found"],
            "merges_linked_by_logo": logo_merge_counts.get(
                "linked_by_logo", 0
            ),
            "merges_not_linked_by_logo": logo_merge_counts.get(
                "not_linked_by_logo", 0
            ),
            "merges_without_logo_data": logo_merge_counts.get(
                "no_logo_data", 0
            ),
            "splits_with_different_logos": logo_split_counts.get(
                "different_logos", 0
            ),
            "splits_sharing_a_logo": logo_split_counts.get(
                "share_a_logo", 0
            ),
            "splits_without_logo_data": logo_split_counts.get(
                "no_logo_data", 0
            ),
        },
        "Number of branches": {
            "unique_branch_id": len(branch_ids),
            "unique_branch_name": len(branch_names),
            "unique_branch_name_case_insensitive": len(branch_names_lower),
            "by_id_multiple_branches": by_id_split["multiple_branches"],
            "by_id_single_branch": by_id_split["single_branch"],
            "by_id_no_branch_id": by_id_split["no_branch_id"],
        },
        "Number of branches after grouping": {
            "unique_branch_id": len(grouped_branch_ids),
            "unique_branch_name": len(grouped_branch_names),
            "unique_branch_name_case_insensitive": len(
                grouped_branch_names_lower
            ),
            "branch_ids_in_multiple_restaurants": len(shared_branch_ids),
            "grouped_multiple_branches": by_group_split["multiple_branches"],
            "grouped_single_branch": by_group_split["single_branch"],
            "grouped_no_branch_id": by_group_split["no_branch_id"],
        },
        "Branches per restaurant (after grouping)": {
            "total_restaurants": total_restaurants,
            "total_branches": total_branches,
            "avg_branches_per_restaurant": avg_branches,
            "ratio_restaurants_to_branches": f"1 : {avg_branches}",
            "restaurants_pct_of_branches": restaurants_pct_of_branches,
            "restaurants_multiple_branches_pct": multi_pct,
            "restaurants_single_branch_pct": single_pct,
        },
        "Cuisines (after grouping)": {
            "unique_cuisines": len(cuisine_counter),
            "restaurants_with_cuisine": with_cuisine,
            "restaurants_without_cuisine": without_cuisine,
        },
        "Locations (shopCity / shopArea)": {
            "unique_cities": len(all_cities),
            "restaurants_with_city": restaurants_with_city,
            "restaurants_without_city": restaurants_without_city,
            "branches_with_city": branches_with_city,
            "branch_city_links": branch_city_links,
            "unique_areas": len(all_areas),
            "restaurants_with_area": restaurants_with_area,
            "branches_with_area": branches_with_area,
            "branches_without_area": all_branches_count - branches_with_area,
            "branch_area_links": branch_area_links,
            "areas_with_single_restaurant": areas_with_single_restaurant,
            "areas_with_multiple_cities": len(areas_multi_city),
            "branches_with_multiple_cities": len(branches_multi_city_details),
            "branches_with_multiple_areas": len(branches_multi_area_details),
        },
        "Some details about duplicates": {
            "ids_with_multiple_names": len(multi_name_ids),
            "names_with_multiple_ids": len(multi_id_names),
            "branch_ids_with_multiple_names": len(multi_name_branch_ids),
            "branch_names_with_multiple_ids": len(multi_id_branch_names),
        },
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for section, values in summary.items():
        print(f"\n{section}")
        for k, v in values.items():
            print(f"{k:<38}: {v}")

    print("\n")

    # ----------------------------------------------------
    # SAVE FILES
    # ----------------------------------------------------
    save_json(SUMMARY_FILE, {
        "summary": summary,
        "branches_per_restaurant_distribution": branches_distribution,
    })
    save_json(RESTAURANTS_FILE, unique_restaurants)
    save_json(RESTAURANTS_GROUPED_FILE, grouped)
    save_json(BRANCHES_FILE, unique_branches)
    save_json(BRANCHES_GROUPED_FILE, branches_grouped)
    save_json(DUPLICATES_FILE, {
        "ids_with_multiple_names": multi_name_ids,
        "names_with_multiple_ids": multi_id_names,
        "branch_ids_with_multiple_names": multi_name_branch_ids,
        "branch_names_with_multiple_ids": multi_id_branch_names,
        "branch_ids_in_multiple_restaurants": shared_branch_ids,
        "branches_with_multiple_cities": branches_multi_city_details,
        "branches_with_multiple_areas": branches_multi_area_details,
        "areas_with_multiple_cities": areas_multi_city,
    })
    save_json(MULTI_NAMES_FILE, merged)
    save_json(MERGED_BY_ID_FILE, merged_by_id)
    save_json(BRANCH_COUNTS_FILE, restaurant_branch_counts)
    save_json(MULTI_BRANCHES_FILE, restaurants_multiple_branches)
    save_json(CUISINE_FILE, cuisine_distribution)
    save_json(CITY_FILE, city_distribution)
    save_json(AREA_BRANCHES_FILE, area_branches_distribution)
    save_json(DENSITY_FILE, density_distribution)
    save_json(COMPARISON_FILE, comparison_file)

    # ----------------------------------------------------
    # SAMPLES (التفاصيل الكاملة في الملفات)
    # ----------------------------------------------------
    print("=" * 70)
    print(f"SAMPLES (first {SAMPLES} only, full details are in the files)")
    print("=" * 70)

    print_block(
        "Grouping: merges the review would apply",
        len(merged_entries),
        [
            f"{e['restaurant']}: merges {e['old_groups_merged']} old groups "
            f"({e['ids']} ids) | logo: {e['logo_evidence']}"
            for e in merged_entries[:COMPARISON_PRINT]
        ],
        COMPARISON_FILE,
    )

    print_block(
        "Grouping: ids the review would separate",
        len(separated_entries),
        [
            f"{e['restaurant']}: split into {len(e['split_into'])} "
            f"({len(e['ids'])} ids) | logo: {e['logo_evidence']}"
            for e in separated_entries[:COMPARISON_PRINT]
        ],
        COMPARISON_FILE,
    )

    print_block(
        "Unique restaurants (id | name)",
        len(unique_restaurants),
        [f"{r['id']} | {r['name']}" for r in unique_restaurants[:SAMPLES]],
        RESTAURANTS_FILE,
    )

    top_grouped = sorted(
        grouped.items(), key=lambda x: -x[1]["unique_branch_ids"]
    )[:SAMPLES]
    print_block(
        "Unique restaurants after grouping (top by branches)",
        len(grouped),
        [
            f"{b} [{g['unique_ids']} ids | "
            f"{g['unique_branch_ids']} branches]"
            for b, g in top_grouped
        ],
        RESTAURANTS_GROUPED_FILE,
    )

    print_block(
        "Unique branches (branchId | branchName)",
        len(unique_branches),
        [
            f"{b['branchId']} | {b['branchName']}"
            for b in unique_branches[:SAMPLES]
        ],
        BRANCHES_FILE,
    )

    top_branches_grouped = sorted(
        branches_grouped.items(), key=lambda x: -x[1]["unique_branch_ids"]
    )[:SAMPLES]
    print_block(
        "Unique branches after grouping (top restaurants)",
        len(branches_grouped),
        [
            f"{b}: {g['unique_branch_ids']} branch ids"
            for b, g in top_branches_grouped
        ],
        BRANCHES_GROUPED_FILE,
    )

    print_block(
        "Branches per restaurant (restaurant_id | restaurant: branches)",
        len(restaurant_branch_counts),
        [
            f"{rid} | {v['restaurant']}: {v['num_branches']} branches"
            for rid, v in list(restaurant_branch_counts.items())[:SAMPLES]
        ],
        BRANCH_COUNTS_FILE,
    )

    print_block(
        "Restaurants with more than one branch",
        len(restaurants_multiple_branches),
        [
            f"{rid} | {v['restaurant']}: {v['num_branches']} branches"
            for rid, v in list(restaurants_multiple_branches.items())[:SAMPLES]
        ],
        MULTI_BRANCHES_FILE,
    )

    print_block(
        "Restaurants per cuisine (top)",
        len(restaurants_per_cuisine),
        [
            f"{c['cuisine']}: {c['restaurants']} restaurants "
            f"({c['percentage_of_restaurants']}%)"
            for c in restaurants_per_cuisine[:CUISINE_SAMPLES]
        ],
        CUISINE_FILE,
    )

    print("\nCuisines per restaurant:")
    for n, count in sorted(cuisines_per_restaurant.items()):
        print(f"  {n} cuisine(s): {count} restaurants")

    # ---- city ----
    print_block(
        "Restaurants per city (city code)",
        len(restaurants_per_city),
        [
            f"city {c['city']}: {c['restaurants']} restaurants "
            f"({c['pct_of_restaurants']}%) | {c['branches']} branches "
            f"({c['pct_of_branches']}%) | {c['areas']} areas"
            for c in restaurants_per_city[:AREA_SAMPLES]
        ],
        CITY_FILE,
    )

    print("\nCities per restaurant:")
    for n, count in sorted(cities_per_restaurant.items()):
        print(f"  {n} city(ies): {count} restaurants")

    # ---- area ----
    print_block(
        "Branches per area (top)",
        len(branches_per_area),
        [
            f"area {a['area']} (city {a['city']}): {a['branches']} branches "
            f"({a['pct_of_branches']}%)"
            for a in branches_per_area[:AREA_SAMPLES]
        ],
        AREA_BRANCHES_FILE,
    )

    print_block(
        "Highest restaurant density areas",
        len(density_list),
        [
            f"area {a['area']} (city {a['city']}): {a['restaurants']} "
            f"restaurants ({a['pct_of_restaurants']}%) | "
            f"{a['branches']} branches | "
            f"avg {a['avg_branches_per_restaurant']} branches/restaurant"
            for a in top_areas
        ],
        DENSITY_FILE,
    )

    print_block(
        f"Lowest restaurant density areas "
        f"({areas_with_single_restaurant} areas have only 1 restaurant)",
        len(density_list),
        [
            f"area {a['area']} (city {a['city']}): {a['restaurants']} "
            f"restaurants ({a['pct_of_restaurants']}%) | "
            f"{a['branches']} branches"
            for a in bottom_areas
        ],
        DENSITY_FILE,
    )

    # ---- الحالات الغريبة في الموقع ----
    print_block(
        "Branches that appear with more than one CITY",
        len(branches_multi_city_details),
        [
            location_line(bid, info)
            for bid, info in list(branches_multi_city_details.items())[
                :MULTI_LOCATION_PRINT
            ]
        ],
        DUPLICATES_FILE,
    )

    print_block(
        "Branches that appear with more than one AREA",
        len(branches_multi_area_details),
        [
            location_line(bid, info)
            for bid, info in list(branches_multi_area_details.items())[
                :MULTI_LOCATION_PRINT
            ]
        ],
        DUPLICATES_FILE,
    )

    print_block(
        "Areas that appear under more than one city (should be 0)",
        len(areas_multi_city),
        [
            f"area {a} -> cities {cities}"
            for a, cities in list(areas_multi_city.items())[
                :MULTI_LOCATION_PRINT
            ]
        ],
        DUPLICATES_FILE,
    )

    # ---- duplicates ----
    print_block(
        "ids with multiple names",
        len(multi_name_ids),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_name_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "names with multiple ids",
        len(multi_id_names),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_id_names.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch ids with multiple names",
        len(multi_name_branch_ids),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_name_branch_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch names with multiple ids",
        len(multi_id_branch_names),
        [
            f"{k} -> {short_list(v)}"
            for k, v in list(multi_id_branch_names.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )
    print_block(
        "branch ids shared by more than one restaurant",
        len(shared_branch_ids),
        [
            f"{bid} -> {short_list(brands)}"
            for bid, brands in list(shared_branch_ids.items())[:SAMPLES]
        ],
        DUPLICATES_FILE,
    )

    # restaurants with several name keys (same id with different names)
    top_by_id = sorted(
        merged_by_id.items(),
        key=lambda x: -len(x[1]["name_variants"]),
    )[:SAMPLES]
    print_block(
        "Restaurants merged because the same id has different names",
        len(merged_by_id),
        [
            f"{b} <- {short_list(info['name_variants'])}"
            for b, info in top_by_id
        ],
        MERGED_BY_ID_FILE,
    )

    # restaurants merged into one (top by number of names)
    top_merged = sorted(
        merged.items(), key=lambda x: -x[1]["unique_names"]
    )[:SAMPLES]

    print(f"\nRESTAURANTS MERGED INTO ONE ({len(merged)})")
    for brand, info in top_merged:
        print(
            f"\n  {brand}  "
            f"[{info['unique_names']} names | "
            f"{info['unique_ids']} ids | "
            f"{info['unique_branch_ids']} branches]"
        )
        if len(info["name_variants"]) > 1:
            print(f"    spelling variants: {info['name_variants']}")
        for n in info["names"][:3]:
            print(f"      - {n}")
        if len(info["names"]) > 3:
            print(f"      ... +{len(info['names']) - 3} more")

    if len(merged) > SAMPLES:
        print(f"\n  ... and {len(merged) - SAMPLES} more")
    print(f"  -> full details: {MULTI_NAMES_FILE}")

    # ----------------------------------------------------
    # SAVED FILES
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{SUMMARY_FILE:<32}: summary numbers")
    print(f"{COMPARISON_FILE:<32}: old vs reviewed grouping (for validation)")
    print(f"{RESTAURANTS_FILE:<32}: {len(unique_restaurants)} {{id, name}}")
    print(f"{RESTAURANTS_GROUPED_FILE:<32}: {len(grouped)} restaurants after grouping")
    print(f"{BRANCHES_FILE:<32}: {len(unique_branches)} {{branchId, branchName}}")
    print(f"{BRANCHES_GROUPED_FILE:<32}: branches per grouped restaurant")
    print(f"{BRANCH_COUNTS_FILE:<32}: {len(restaurant_branch_counts)} restaurants -> num of branches")
    print(f"{MULTI_BRANCHES_FILE:<32}: {len(restaurants_multiple_branches)} restaurants with more than one branch")
    print(f"{CUISINE_FILE:<32}: restaurants per cuisine")
    print(f"{CITY_FILE:<32}: restaurants + branches per city")
    print(f"{AREA_BRANCHES_FILE:<32}: branches per area (with city)")
    print(f"{DENSITY_FILE:<32}: restaurant density per area (top / bottom)")
    print(f"{DUPLICATES_FILE:<32}: multiple-mapping lists + odd locations")
    print(f"{MULTI_NAMES_FILE:<32}: {len(merged)} restaurants with multiple names")
    print(f"{MERGED_BY_ID_FILE:<32}: {len(merged_by_id)} restaurants with several name keys")


if __name__ == "__main__":
    main()