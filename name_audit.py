import os
import re
import csv
import json
import bisect
import difflib
import unicodedata
from collections import defaultdict, Counter

import boto3

R2_PREFIX = os.environ.get(
    "R2_PREFIX",
    "merged-restaurant-info/year=2025/month=09/day=17/"
)

# ---------------- output files ----------------
OVERVIEW_FILE = "name_review_files_overview.json"
UNIQUE_FILE = "name_review_unique_restaurants.json"
CANDIDATES_JSON_FILE = "name_review_candidates.json"
CANDIDATES_CSV_FILE = "name_review_candidates.csv"
SUMMARY_FILE = "name_review_summary.json"

# أعمدة ملف المراجعة (غيّريها من هنا لو عايزة)
# candidate_id = رقم المجموعة (بيجمع الصفوف اللي ممكن تكون نفس المطعم)
COLUMNS = [
    "candidate_id",
    "id",
    "name",
    "restaurantSlug",
    "branchUrl",
    "branchName",
    "grouping_reason",
    "shopCity",
    "logo",
    "strength",
    "validation",
]

# ---------------- thresholds (عدّليهم من هنا) ----------------
MIN_HEAD_CHARS = 3            # أقل عدد حروف لاسم بنقارن بيه البداية
MIN_SUBSTRING_LEN = 5         # أقل عدد حروف لاسم نبحث عنه جوه اسم تاني
CHAIN_MIN_NAMES = 5           # اسم ظاهر بـ 5 أسماء أو أكتر = سلسلة (مفيش حد لحجمها)
CLUSTER_MAX_ENTRIES = 40      # أكبر حجم لمجموعة متوسطة/ضعيفة (الأكبر بيتسجل في الـ summary)
FUZZY_MIN_RATIO = 0.85        # أقل تشابه إملائي
FUZZY_MIN_LEN = 4             # أقل طول للاسم (من غير مسافات) في التشابه الإملائي
TOKEN_JACCARD_MIN = 0.5       # أقل تشابه كلمات
TOKEN_MAX_POSTINGS = 40       # كلمة موجودة في أكتر من كده اسم بنتجاهلها في تشابه الكلمات
SLUG_PREFIX_MIN_LEN = 5       # أقل طول لـ slug بيبدأ بيه slug تاني
ACRONYM_MIN_LEN = 2           # أقل وأكبر طول للاختصار (KFM)
ACRONYM_MAX_LEN = 6
IMAGE_MAX_IDS = 15            # صورة (لوجو/hero) مشتركة بين أكتر من كده ids...
IMAGE_MAX_DISTINCT_NAMES = 12  # ... وبأسماء مختلفة أكتر من كده = غالبًا صورة افتراضية
SAMPLE_BRANCH_NAMES = 3       # كام branchName بيتكتبوا جنب كل مطعم

# كلمات مش بنحسبها كاسم مميز (بداية/كلمة مشتركة)
STOP_WORDS = {"al", "el", "the", "a", "an", "of", "and", "by", "at", "in"}

# كلمات بتتشال في طريقة "نفس الاسم من غير كلمات عامة" (غيّريها براحتك)
GENERIC_WORDS = STOP_WORDS | {
    "restaurant", "restaurants", "cafe", "coffee", "kitchen", "express",
    "station", "co", "company", "store", "shop", "tgo", "tmp", "dh", "dhk",
    "dt", "drive", "thru", "branch",
}

SAMPLES = 5
TOP_N_PRINT = 10

STRENGTH_RANK = {"strong": 0, "medium": 1, "weak": 2}

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)
BUCKET = os.environ["CF_R2_BUCKET_NAME"]

APOSTROPHES = re.compile(r"[\'`´‘’ʼ′ʻ]")
ARABIC_MARKS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
ARABIC_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})

# الفاصل: فاصلة، أو " - " (بمسافات)، أو قوس، أو | ، أو " / "
SEP_RE = re.compile(r"\s*(?:,|\(|\||\s/\s|\s[-–—]\s)\s*")


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
    """unicode + حروف عربي موحدة + حذف الـ apostrophe + مسافات + lowercase."""
    text = unicodedata.normalize("NFKC", text)
    text = ARABIC_MARKS.sub("", text)
    text = text.translate(ARABIC_MAP)
    text = APOSTROPHES.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def tokens(text):
    """كلمات النص (حروف وأرقام بس) بحروف صغيرة."""
    return re.findall(r"[^\W_]+", normalize_text(text))


def squash(text):
    """النص من غير مسافات ولا علامات."""
    return "".join(tokens(text))


def name_part(name):
    """الجزء الأول من الاسم قبل , أو ' - ' أو قوس (دا اسم المطعم)."""
    part = SEP_RE.split(name, maxsplit=1)[0].strip()
    return part or name.strip()


def phonetic_key(squashed):
    """مفتاح "بيتنطق زي بعض": بيوحّد التهجئة (sh/kh/ph، حروف مكررة، حروف العلة)."""
    s = re.sub(r"(.)\1+", r"\1", squashed)
    for a, b in (
        ("ph", "f"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("sh", "s"),
        ("ck", "k"), ("ou", "u"), ("oo", "u"), ("ee", "i"), ("ei", "i"),
        ("ey", "i"), ("ai", "i"), ("aa", "a"), ("ae", "a"),
    ):
        s = s.replace(a, b)
    s = s.translate(str.maketrans({
        "q": "k", "c": "k", "z": "s", "y": "i", "e": "i", "o": "u", "x": "k",
    }))
    s = re.sub(r"(.)\1+", r"\1", s)
    return s.rstrip("h")


def normalize_url(url):
    if not url:
        return None
    u = str(url).strip().lower()
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/") or None


def top_of(counter):
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def find_sub(seq, sub):
    """أول مكان بتظهر فيه sub جوه seq (tuples)، أو -1."""
    n, m = len(seq), len(sub)
    if m == 0 or m > n:
        return -1
    for i in range(n - m + 1):
        if seq[i:i + m] == sub:
            return i
    return -1


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def short_list(values, n=5):
    values = list(values)
    if len(values) <= n:
        return str(values)
    return str(values[:n])[:-1] + f", ... +{len(values) - n} more]"


def print_block(title, total, lines, filename):
    print(f"\n{title} ({total})")
    for line in lines:
        print(f"  {line}")
    if total > len(lines):
        print(f"  ... and {total - len(lines)} more")
    print(f"  -> full details: {filename}")


# ============================================================
# MAIN
# ============================================================

def main():
    # ====================================================
    # الخطوة 1: اقرا كل الـ files وشوف كام record في كل واحد
    # ====================================================
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                keys.append(obj["Key"])
    keys.sort()
    print(f"Files found: {len(keys)}")

    # بيانات كل id (بعد ما نشيل التكرار)
    all_ids = set()
    id_names = defaultdict(Counter)
    id_slugs = defaultdict(Counter)
    id_logos = defaultdict(Counter)
    id_heroes = defaultdict(Counter)
    id_urls = {}
    id_cities = defaultdict(set)
    id_branch_names = defaultdict(Counter)
    id_branch_ids = defaultdict(set)
    branch_to_ids = defaultdict(set)
    bname_to_ids = defaultdict(set)
    id_branch_keys = defaultdict(lambda: defaultdict(set))
    branch_key_example = {}

    files_overview = []
    total_records = 0
    records_with_id = 0
    records_without_id = 0
    records_without_name = 0
    all_names = set()

    for i, key in enumerate(keys, start=1):
        records = load_records(key)
        n = len(records)
        total_records += n

        file_ids = set()
        file_with_id = 0

        for r in records:
            _id = clean(r.get("id"))
            if not _id:
                records_without_id += 1
                continue

            name = clean(r.get("name"))
            records_with_id += 1
            file_with_id += 1
            file_ids.add(_id)
            all_ids.add(_id)

            if name:
                id_names[_id][name] += 1
                all_names.add(name)
            else:
                records_without_name += 1

            slug = clean(r.get("restaurantSlug"))
            if slug:
                id_slugs[_id][slug] += 1
            logo = clean(r.get("logo"))
            if logo:
                id_logos[_id][logo] += 1
            hero = clean(r.get("heroImage"))
            if hero:
                id_heroes[_id][hero] += 1
            url = clean(r.get("branchUrl"))
            if url and _id not in id_urls:
                id_urls[_id] = url
            city = norm_code(r.get("shopCity"))
            if city:
                id_cities[_id].add(city)

            b_id = clean(r.get("branchId"))
            b_name = clean(r.get("branchName"))
            if b_id:
                branch_to_ids[b_id].add(_id)
                id_branch_ids[_id].add(b_id)
            if b_name:
                id_branch_names[_id][b_name] += 1
                nb = normalize_text(b_name)
                if nb:
                    bname_to_ids[nb].add(_id)
                bk = normalize_text(name_part(b_name))
                if bk:
                    id_branch_keys[_id][bk].add(b_id or b_name)
                    branch_key_example.setdefault(bk, b_name)

        files_overview.append({
            "file": key,
            "records": n,
            "records_with_id": file_with_id,
            "unique_ids_in_file": len(file_ids),
        })
        print(f"[{i}/{len(keys)}] {key} -> {n} records, "
              f"{len(file_ids)} unique ids")

    # ====================================================
    # الخطوة 2 و 3: المجموع، وشيل التكرار (unique id)
    # ====================================================
    counts = [f["records"] for f in files_overview]
    pairs_unique = sum(len(c) for c in id_names.values())
    ids_with_names = sorted(id_names, key=sort_key)

    overview = {
        "step_1_files_read": len(keys),
        "records_per_file_min": min(counts) if counts else 0,
        "records_per_file_max": max(counts) if counts else 0,
        "records_per_file_avg": round(sum(counts) / len(counts), 1)
        if counts else 0,
        "step_2_total_records_all_files": total_records,
        "records_with_id": records_with_id,
        "records_without_id": records_without_id,
        "records_without_name": records_without_name,
        "step_3_unique_ids": len(all_ids),
        "duplicate_records_removed": records_with_id - len(all_ids),
        "unique_ids_with_a_name": len(id_names),
        "unique_id_name_pairs": pairs_unique,
        "unique_names": len(all_names),
        "unique_names_case_insensitive": len({n.lower() for n in all_names}),
        "ids_with_more_than_one_name": sum(
            1 for c in id_names.values() if len(c) > 1
        ),
        "files": files_overview,
    }
    save_json(OVERVIEW_FILE, overview)

    save_json(UNIQUE_FILE, [
        {
            "id": rid,
            "name": top_of(id_names[rid]),
            "names": sorted(id_names[rid]),
            "restaurantSlug": top_of(id_slugs.get(rid)),
            "branchUrl": id_urls.get(rid),
            "shopCity": ",".join(sorted(id_cities.get(rid, ()), key=sort_key)),
            "logo": top_of(id_logos.get(rid)),
        }
        for rid in sorted(all_ids, key=sort_key)
    ])

    print("\n" + "=" * 70)
    print("STEPS 1-3: FILES, RECORDS, UNIQUE IDS")
    print("=" * 70)
    print(f"files read                         : {len(keys)}")
    print(f"records per file (min / avg / max) : "
          f"{overview['records_per_file_min']} / "
          f"{overview['records_per_file_avg']} / "
          f"{overview['records_per_file_max']}")
    print(f"total records (all files together) : {total_records}")
    print(f"records without id                 : {records_without_id}")
    print(f"records without name               : {records_without_name}")
    print(f"unique ids (after removing dups)   : {len(all_ids)}")
    print(f"duplicate records removed          : "
          f"{overview['duplicate_records_removed']}")
    print(f"unique (id, name) pairs            : {pairs_unique}")
    print(f"unique names                       : {len(all_names)}")
    print(f"ids with more than one name        : "
          f"{overview['ids_with_more_than_one_name']}")

    # ====================================================
    # الخطوة 4: كل الأسماء اللي ممكن تكون متشابهة (من غير grouping)
    # ====================================================
    entries = []
    for rid in ids_with_names:
        slug = top_of(id_slugs.get(rid))
        logo = top_of(id_logos.get(rid))
        branch_samples = " | ".join(
            b for b, _c in id_branch_names.get(rid, Counter()).most_common(
                SAMPLE_BRANCH_NAMES
            )
        )
        cities = ",".join(sorted(id_cities.get(rid, ()), key=sort_key))
        for name in sorted(id_names[rid]):
            part = name_part(name)
            toks = tuple(tokens(part)) or tuple(tokens(name))
            entries.append({
                "id": rid,
                "name": name,
                "part": part,
                "npart": normalize_text(part),
                "nfull": normalize_text(name),
                "sq_part": squash(part) or squash(name),
                "sq_full": squash(name),
                "tokens": toks,
                "ftokens": tuple(tokens(name)),
                "slug": slug.lower() if slug else None,
                "slug_raw": slug,
                "url": id_urls.get(rid),
                "logo": logo,
                "branch_name": branch_samples,
                "city": cities,
            })

    by_id = defaultdict(list)
    by_name = defaultdict(list)
    by_lower = defaultdict(list)
    by_sq_full = defaultdict(list)
    by_npart = defaultdict(list)
    by_sq_part = defaultdict(list)
    by_tokens = defaultdict(list)

    for i, e in enumerate(entries):
        by_id[e["id"]].append(i)
        by_name[e["name"]].append(i)
        by_lower[e["name"].lower().strip()].append(i)
        if e["sq_full"]:
            by_sq_full[e["sq_full"]].append(i)
        by_npart[e["npart"]].append(i)
        if e["sq_part"]:
            by_sq_part[e["sq_part"]].append(i)
        by_tokens[e["tokens"]].append(i)

    def distinct_ids(idxs):
        return len({entries[i]["id"] for i in idxs})

    def distinct_names(idxs):
        return len({entries[i]["name"] for i in idxs})

    def brand_names_count(tok_tuple):
        """كام اسم مختلف اسمه بالظبط الكلمات دي (سلسلة؟)."""
        return distinct_names(by_tokens.get(tok_tuple, ()))

    clusters = {}
    oversized = []
    generic_skipped = []

    def add(method, strength, idxs, label=None, note="",
            need_two_ids=True, exempt=False):
        idxs = frozenset(idxs)
        if len(idxs) < 2:
            return
        if need_two_ids and distinct_ids(idxs) < 2:
            return
        if (strength != "strong" and not exempt
                and len(idxs) > CLUSTER_MAX_ENTRIES):
            oversized.append({
                "method": method, "label": label, "entries": len(idxs),
                "ids": distinct_ids(idxs),
            })
            return
        c = clusters.setdefault(idxs, {"methods": {}})
        m = c["methods"].setdefault(
            method, {"strength": strength, "notes": []}
        )
        if note and note not in m["notes"]:
            m["notes"].append(note)

    # ---- 1) نفس الـ id بأسماء مختلفة ----
    for rid, idxs in by_id.items():
        if len(idxs) >= 2:
            add("same id, different names", "strong", idxs,
                need_two_ids=False)

    # ---- 2) نفس الاسم بالظبط بـ ids مختلفة ----
    for _name, idxs in by_name.items():
        add("same name, different ids", "strong", idxs)

    # ---- 3) نفس الاسم مع اختلاف lower / upper case ----
    for _k, idxs in by_lower.items():
        if distinct_names(idxs) >= 2:
            add("lower/upper case difference", "strong", idxs,
                need_two_ids=False)

    # ---- 4) نفس الحروف لو شيلنا المسافات والعلامات ----
    for _k, idxs in by_sq_full.items():
        if len({entries[i]["nfull"] for i in idxs}) >= 2:
            add("same letters without spaces/punctuation", "strong", idxs,
                need_two_ids=False)

    # ---- 5) نفس اسم المطعم قبل , أو - أو القوس ----
    for _k, idxs in by_npart.items():
        add("same restaurant name before , / - / (", "strong", idxs)

    # ---- 6) نفس اسم المطعم من غير مسافات وعلامات ----
    for _k, idxs in by_sq_part.items():
        if len({entries[i]["npart"] for i in idxs}) >= 2:
            add("same restaurant name without spaces/punctuation", "strong",
                idxs, need_two_ids=False)

    # ---- 7) اسم جوه اسم تاني / بيبدأ بيه ----
    sq_items = sorted(by_sq_part.items(), key=lambda kv: (len(kv[0]), kv[0]))
    sq_keys = [k for k, _ in sq_items]
    sq_lens = [len(k) for k in sq_keys]

    spec = {
        "prefix_words": (
            "name starts with another restaurant's name (whole words)",
            "medium"),
        "inside_words": (
            "name is inside another name (whole words)", "medium"),
        "prefix_letters": (
            "name starts with another name's letters (inside a word)", "weak"),
        "inside_letters": (
            "name is inside another name (letters only)", "weak"),
    }

    for a_key, a_idxs in sq_items:
        if len(a_key) < MIN_SUBSTRING_LEN:
            continue

        tok_a = entries[a_idxs[0]]["tokens"]
        start = bisect.bisect_right(sq_lens, len(a_key))

        if all(t in GENERIC_WORDS for t in tok_a):
            hits_n = sum(
                1 for b_pos in range(start, len(sq_keys))
                if a_key in sq_keys[b_pos]
            )
            if hits_n:
                generic_skipped.append({
                    "word": " ".join(tok_a), "kind": "name inside names",
                    "names_containing_it": hits_n,
                })
            continue

        cats = {k: [] for k in spec}
        for b_pos in range(start, len(sq_keys)):
            b_key = sq_keys[b_pos]
            if a_key not in b_key:
                continue
            tok_b = entries[sq_items[b_pos][1][0]]["tokens"]
            pos = find_sub(tok_b, tok_a)
            if pos == 0:
                cats["prefix_words"].append(b_pos)
            elif pos > 0:
                cats["inside_words"].append(b_pos)
            elif b_key.startswith(a_key):
                cats["prefix_letters"].append(b_pos)
            else:
                cats["inside_letters"].append(b_pos)

        is_chain = distinct_names(a_idxs) >= CHAIN_MIN_NAMES
        label = entries[a_idxs[0]]["part"]

        for cat, b_positions in cats.items():
            if not b_positions:
                continue
            members = list(a_idxs)
            for b_pos in b_positions:
                members.extend(sq_items[b_pos][1])
            method, strength = spec[cat]
            add(method, strength, members, label=label,
                note=f"name: {label}", exempt=is_chain)

    # ---- 8) مطاعم بتبدأ بنفس الكلمات ----
    for n_words, strength in ((1, "weak"), (2, "medium"), (3, "medium")):
        groups = defaultdict(list)
        for i, e in enumerate(entries):
            if len(e["tokens"]) >= n_words:
                groups[e["tokens"][:n_words]].append(i)

        for head, idxs in groups.items():
            if len(idxs) < 2:
                continue
            if (all(t in GENERIC_WORDS for t in head)
                    or len("".join(head)) < MIN_HEAD_CHARS):
                if n_words == 1:
                    generic_skipped.append({
                        "word": " ".join(head),
                        "kind": "shared first word",
                        "names_containing_it": len(idxs),
                    })
                continue
            add(f"share the first {n_words} word(s)", strength, idxs,
                label=" ".join(head),
                exempt=brand_names_count(head) >= CHAIN_MIN_NAMES)

    # ---- 9) نفس الكلمات بترتيب مختلف ----
    groups = defaultdict(list)
    for i, e in enumerate(entries):
        if len(e["tokens"]) >= 2:
            groups[tuple(sorted(e["tokens"]))].append(i)
    for _k, idxs in groups.items():
        if len({entries[i]["npart"] for i in idxs}) >= 2:
            add("same words in a different order", "medium", idxs)

    # ---- 10) نفس الاسم من غير الكلمات العامة (Al / TGO / Cafe / أرقام) ----
    groups = defaultdict(list)
    for i, e in enumerate(entries):
        stripped = tuple(
            t for t in e["tokens"] if t not in GENERIC_WORDS and not t.isdigit()
        )
        if stripped and len("".join(stripped)) >= MIN_HEAD_CHARS:
            groups[stripped].append(i)
    for stripped, idxs in groups.items():
        if len({entries[i]["npart"] for i in idxs}) >= 2:
            add("same name after removing generic words / numbers", "medium",
                idxs, note="name: " + " ".join(stripped),
                exempt=brand_names_count(stripped) >= CHAIN_MIN_NAMES)

    # ---- 11) أسماء بتشترك في معظم الكلمات ----
    token_sets = []
    postings = defaultdict(set)
    for i, e in enumerate(entries):
        ts = {t for t in e["tokens"]
              if t not in GENERIC_WORDS and len(t) >= 3 and not t.isdigit()}
        token_sets.append(ts)
        for t in ts:
            postings[t].add(i)

    seen_pairs = set()
    for _t, idxs in postings.items():
        if len(idxs) < 2 or len(idxs) > TOKEN_MAX_POSTINGS:
            continue
        idxs = sorted(idxs)
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                a, b = idxs[x], idxs[y]
                if (a, b) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
                if entries[a]["npart"] == entries[b]["npart"]:
                    continue
                inter = token_sets[a] & token_sets[b]
                union = token_sets[a] | token_sets[b]
                if union and len(inter) / len(union) >= TOKEN_JACCARD_MIN:
                    add("share most of the words", "medium", (a, b),
                        note="shared: " + " ".join(sorted(inter)))

    # ---- 12) تشابه إملائي ----
    sm = difflib.SequenceMatcher(None)
    n_keys = len(sq_keys)
    fuzzy_pairs = 0
    for i, a_key in enumerate(sq_keys):
        if len(a_key) < FUZZY_MIN_LEN:
            continue
        sm.set_seq2(a_key)
        max_len = int(len(a_key) / FUZZY_MIN_RATIO) + 1
        j = i + 1
        while j < n_keys and sq_lens[j] <= max_len:
            b_key = sq_keys[j]
            sm.set_seq1(b_key)
            if (sm.real_quick_ratio() >= FUZZY_MIN_RATIO
                    and sm.quick_ratio() >= FUZZY_MIN_RATIO):
                ratio = sm.ratio()
                if ratio >= FUZZY_MIN_RATIO:
                    fuzzy_pairs += 1
                    add("similar spelling", "medium",
                        sq_items[i][1] + sq_items[j][1],
                        note=f"similarity {ratio:.2f}")
            j += 1

    # ---- 13) بيتنطقوا زي بعض (Shubak / Shubbak / Zwara / Zwarah) ----
    groups = defaultdict(list)
    for key_sq, idxs in sq_items:
        pk = phonetic_key(key_sq)
        if len(pk) >= FUZZY_MIN_LEN - 1:
            groups[pk].extend(idxs)
    for pk, idxs in groups.items():
        if len({entries[i]["sq_part"] for i in idxs}) >= 2:
            add("sounds similar (spelling variants)", "medium", idxs,
                note="key: " + pk)

    # ---- 14) اختصار (KFM = Kuwait Flour Mills) ----
    acronyms = defaultdict(set)
    for i, e in enumerate(entries):
        toks = [t for t in e["ftokens"] if t not in STOP_WORDS]
        for ln in range(ACRONYM_MIN_LEN, min(ACRONYM_MAX_LEN, len(toks)) + 1):
            acronyms["".join(t[0] for t in toks[:ln])].add(i)
    for sq_key, idxs in by_sq_part.items():
        if ACRONYM_MIN_LEN <= len(sq_key) <= ACRONYM_MAX_LEN \
                and sq_key in acronyms:
            if len(entries[idxs[0]]["tokens"]) != 1:
                continue
            others = [i for i in acronyms[sq_key] if i not in idxs]
            add("abbreviation of another name", "medium",
                list(idxs) + others, note=f"abbreviation: {sq_key}")

    # ---- 15) نفس الـ restaurantSlug ----
    slug_groups = defaultdict(list)
    for i, e in enumerate(entries):
        if e["slug"]:
            slug_groups[e["slug"]].append(i)

    for slug, idxs in slug_groups.items():
        add("same restaurantSlug", "strong", idxs, note="slug: " + slug)

    root_groups = defaultdict(list)
    for slug, idxs in slug_groups.items():
        root = re.sub(r"[-_\d]+$", "", slug)
        if root:
            root_groups[root].extend(idxs)
    for root, idxs in root_groups.items():
        if len({entries[i]["slug"] for i in idxs}) >= 2:
            add("same restaurantSlug after removing trailing numbers",
                "medium", idxs, note="slug: " + root)

    slug_sorted = sorted(slug_groups)
    for pos, a in enumerate(slug_sorted):
        if len(a) < SLUG_PREFIX_MIN_LEN:
            continue
        members = list(slug_groups[a])
        found = False
        j = pos + 1
        while j < len(slug_sorted) and slug_sorted[j].startswith(a):
            members.extend(slug_groups[slug_sorted[j]])
            found = True
            j += 1
        if found:
            add("restaurantSlug starts with another slug", "weak", members,
                note="slug: " + a)

    # ---- 16 و 17) نفس اللوجو / نفس صورة الـ hero ----
    placeholder_images = []

    def image_method(per_id, method):
        groups_img = defaultdict(set)
        for rid in ids_with_names:
            for img in per_id.get(rid, ()):
                u = normalize_url(img)
                if u:
                    groups_img[u].add(rid)
        for u, rids in groups_img.items():
            if len(rids) < 2:
                continue
            idxs = [i for rid in rids for i in by_id[rid]]
            names_n = len({entries[i]["npart"] for i in idxs})
            if len(rids) > IMAGE_MAX_IDS and names_n > IMAGE_MAX_DISTINCT_NAMES:
                placeholder_images.append({
                    "kind": method, "image": u, "ids": len(rids),
                    "distinct_names": names_n,
                })
                continue
            add(method, "strong", idxs, note=u)

    image_method(id_logos, "same logo")
    image_method(id_heroes, "same hero image")

    # ---- 18) نفس الـ branchId ظهر تحت أكتر من id ----
    for bid, rids in branch_to_ids.items():
        rids = {r for r in rids if r in by_id}
        if len(rids) >= 2:
            idxs = [i for rid in rids for i in by_id[rid]]
            add("share the same branchId", "strong", idxs,
                note=f"branchId: {bid}")

    # ---- 19) نفس الـ branchName عند أكتر من id ----
    for nb, rids in bname_to_ids.items():
        rids = {r for r in rids if r in by_id}
        if len(rids) >= 2:
            idxs = [i for rid in rids for i in by_id[rid]]
            add("same branchName", "medium", idxs, note=f"branchName: {nb}")

    # ---- 20) branchName بتاع فروعه هو اسم مطعم تاني ----
    for rid, keys_map in id_branch_keys.items():
        if rid not in by_id:
            continue
        own = {entries[i]["npart"] for i in by_id[rid]}
        own_sq = {entries[i]["sq_part"] for i in by_id[rid]}
        for bk, bids in keys_map.items():
            if bk in own or squash(bk) in own_sq:
                continue
            others = list(by_npart.get(bk, ()))
            if not others:
                others = list(by_sq_part.get(squash(bk), ()))
            others = [i for i in others if entries[i]["id"] != rid]
            if others:
                add("branchName of its branches is another restaurant's name",
                    "medium", list(by_id[rid]) + others,
                    note=f"branchName e.g. '{branch_key_example.get(bk)}' "
                         f"({len(bids)} branches)",
                    exempt=True)

    # ====================================================
    # ترتيب المجموعات وكتابة الملف
    # ====================================================
    final = []
    for idxs, c in clusters.items():
        strength = min(
            (m["strength"] for m in c["methods"].values()),
            key=lambda s: STRENGTH_RANK[s],
        )
        parts = Counter(entries[i]["part"] for i in idxs)
        label = sorted(
            parts.items(), key=lambda x: (-x[1], len(x[0]), x[0])
        )[0][0]

        method_items = sorted(
            c["methods"].items(),
            key=lambda kv: (STRENGTH_RANK[kv[1]["strength"]], kv[0]),
        )
        reason = "; ".join(
            m + (f" [{' | '.join(v['notes'][:2])}]" if v["notes"] else "")
            for m, v in method_items
        )

        final.append({
            "idxs": sorted(
                idxs, key=lambda i: (entries[i]["name"].lower(),
                                     sort_key(entries[i]["id"]))
            ),
            "label": label,
            "strength": strength,
            "methods": [m for m, _ in method_items],
            "method_strengths": {m: v["strength"] for m, v in method_items},
            "reason": reason,
            "ids": distinct_ids(idxs),
        })

    final.sort(key=lambda f: (
        STRENGTH_RANK[f["strength"]], -len(f["methods"]),
        f["label"].lower(), f["idxs"][0],
    ))

    rows = []
    for n, f in enumerate(final, start=1):
        f["candidate_id"] = f"C{n:04d}"
        for i in f["idxs"]:
            e = entries[i]
            rows.append({
                "candidate_id": f["candidate_id"],
                "id": e["id"],
                "name": e["name"],
                "restaurantSlug": e["slug_raw"],
                "branchUrl": e["url"],
                "branchName": e["branch_name"],
                "grouping_reason": f["reason"],
                "shopCity": e["city"],
                "logo": e["logo"],
                "strength": f["strength"],
                "validation": "",
            })

    with open(CANDIDATES_CSV_FILE, "w", newline="",
              encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # ---- إحصائيات ----
    ids_in_candidates = {r["id"] for r in rows}
    ids_with_name_count = len(id_names)

    method_stats = defaultdict(lambda: {
        "strength": None, "clusters": 0, "ids": set(),
    })
    for f in final:
        for m in f["methods"]:
            ms = method_stats[m]
            ms["strength"] = f["method_strengths"][m]
            ms["clusters"] += 1
            for i in f["idxs"]:
                ms["ids"].add(entries[i]["id"])

    methods_table = sorted(
        (
            {
                "method": m,
                "strength": s["strength"],
                "clusters": s["clusters"],
                "ids_covered": len(s["ids"]),
            }
            for m, s in method_stats.items()
        ),
        key=lambda x: (STRENGTH_RANK[x["strength"]], -x["clusters"]),
    )

    by_strength = Counter(f["strength"] for f in final)
    generic_sorted = sorted(
        generic_skipped, key=lambda x: -x["names_containing_it"]
    )
    oversized_sorted = sorted(oversized, key=lambda x: -x["entries"])
    placeholder_images.sort(key=lambda x: -x["ids"])

    coverage = {
        "unique_ids_with_a_name": ids_with_name_count,
        "ids_in_at_least_one_candidate": len(ids_in_candidates),
        "pct_of_ids_in_candidates": round(
            len(ids_in_candidates) * 100 / ids_with_name_count, 2
        ) if ids_with_name_count else 0,
        "ids_in_no_candidate": ids_with_name_count - len(ids_in_candidates),
    }

    candidates_summary = {
        "candidate_groups": len(final),
        "strong": by_strength.get("strong", 0),
        "medium": by_strength.get("medium", 0),
        "weak": by_strength.get("weak", 0),
        "rows": len(rows),
        "similar_spelling_pairs_found": fuzzy_pairs,
        "oversized_groups_not_listed": len(oversized_sorted),
        "generic_words_not_listed": len(generic_sorted),
        "placeholder_images_not_listed": len(placeholder_images),
    }

    save_json(CANDIDATES_JSON_FILE, {
        "note": (
            "Every set of restaurants whose names may be similar. Nothing is "
            "grouped. validation is empty for the manual review. strength: "
            "strong = deterministic evidence (same id/name/logo/slug...), "
            "medium = probable, weak = loose (short shared start or letters)."
        ),
        "columns": COLUMNS,
        "records_count": len(rows),
        "records": rows,
    })

    save_json(SUMMARY_FILE, {
        "steps_1_to_3": {k: v for k, v in overview.items() if k != "files"},
        "candidates": candidates_summary,
        "coverage": coverage,
        "methods": methods_table,
        "thresholds": {
            "min_head_chars": MIN_HEAD_CHARS,
            "min_substring_len": MIN_SUBSTRING_LEN,
            "chain_min_names": CHAIN_MIN_NAMES,
            "cluster_max_entries": CLUSTER_MAX_ENTRIES,
            "fuzzy_min_ratio": FUZZY_MIN_RATIO,
            "token_jaccard_min": TOKEN_JACCARD_MIN,
            "token_max_postings": TOKEN_MAX_POSTINGS,
            "slug_prefix_min_len": SLUG_PREFIX_MIN_LEN,
            "image_max_ids": IMAGE_MAX_IDS,
            "image_max_distinct_names": IMAGE_MAX_DISTINCT_NAMES,
            "generic_words": sorted(GENERIC_WORDS),
        },
        "oversized_groups_not_listed": oversized_sorted,
        "generic_words_not_listed": generic_sorted,
        "placeholder_images_not_listed": placeholder_images,
    })

    # ----------------------------------------------------
    # print
    # ----------------------------------------------------
    print("\n" + "=" * 70)
    print("STEP 4: SIMILAR-NAME CANDIDATES (nothing is grouped)")
    print("=" * 70)
    for k, v in candidates_summary.items():
        print(f"{k:<34}: {v}")

    print("\nCoverage")
    for k, v in coverage.items():
        print(f"{k:<34}: {v}")

    print("\nCandidate groups per method (a group can have several methods)")
    for m in methods_table:
        print(f"  [{m['strength']}] {m['method']}: {m['clusters']} groups, "
              f"{m['ids_covered']} ids")

    def sample_lines(strength, limit):
        out = []
        for f in final:
            if f["strength"] != strength:
                continue
            names_ = [entries[i]["name"] for i in f["idxs"]]
            out.append(
                f"{f['candidate_id']} {f['label']} ({f['ids']} ids): "
                f"{short_list(names_, 4)} | {', '.join(f['methods'][:2])}"
            )
            if len(out) >= limit:
                break
        return out

    for strength in ("strong", "medium", "weak"):
        print_block(
            f"Samples: {strength} candidates",
            by_strength.get(strength, 0),
            sample_lines(strength, TOP_N_PRINT),
            CANDIDATES_CSV_FILE,
        )

    print_block(
        "Groups too big to list (medium/weak, not rows in the file)",
        len(oversized_sorted),
        [f"{o['label']} [{o['method']}]: {o['entries']} entries"
         for o in oversized_sorted[:SAMPLES]],
        SUMMARY_FILE,
    )
    print_block(
        "Generic words not used for matching",
        len(generic_sorted),
        [f"'{g['word']}' ({g['kind']}): {g['names_containing_it']} names"
         for g in generic_sorted[:SAMPLES]],
        SUMMARY_FILE,
    )
    print_block(
        "Images shared by many unrelated restaurants (probably a default image)",
        len(placeholder_images),
        [f"{p['kind']} {p['image']}: {p['ids']} ids, "
         f"{p['distinct_names']} different names"
         for p in placeholder_images[:SAMPLES]],
        SUMMARY_FILE,
    )

    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)
    print(f"{OVERVIEW_FILE:<40}: records per file + totals")
    print(f"{UNIQUE_FILE:<40}: {len(all_ids)} unique restaurants (by id)")
    print(f"{CANDIDATES_CSV_FILE:<40}: {len(rows)} rows for the manual review")
    print(f"{CANDIDATES_JSON_FILE:<40}: same rows as JSON")
    print(f"{SUMMARY_FILE:<40}: counts, methods, thresholds")


if __name__ == "__main__":
    main()