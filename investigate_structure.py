"""
investigate_structure.py
------------------------
Investigation script: shows the folder structure under R2_PREFIX (default: restaurant-info/year=2025/month=09/).

For every folder directly under the prefix (the days) it prints
  - how many folders it contains
  - the name of each of those folders and how many files it holds (everything inside it, sub-folders included)
  - any file sitting directly in the day folder (outside every sub-folder)
then a one-line-per-day summary, and the folder names that appear in more than one day
(with the number of days and which days).

Nothing is downloaded or modified. The printed output is also saved to structure_report.txt.
"""

import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import boto3

R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PREFIX = os.environ.get("R2_PREFIX", "restaurant-info/year=2025/month=09/")
if not R2_PREFIX.endswith("/"):
    R2_PREFIX += "/"
WORKERS = int(os.environ.get("WORKERS", "16"))  # parallel listing of the sub-folders
OUT_DIR = os.environ.get("OUT_DIR", "structure_output")

s3 = boto3.client(
    "s3", endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY, region_name="auto",
)
LINES = []


def out(text=""):
    print(text)
    LINES.append(str(text))


def list_level(prefix):
    """Folders and files directly under `prefix` (one level only)."""
    folders, files = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix=prefix, Delimiter="/"):
        folders += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        files += [o for o in page.get("Contents", []) if o["Key"] != prefix]
    return folders, files


def count_files(prefix):
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
        n += sum(1 for o in page.get("Contents", []) if not o["Key"].endswith("/"))
    return n


def name_of(prefix):
    return prefix.rstrip("/").split("/")[-1]


def main():
    out(f"Bucket : {R2_BUCKET}")
    out(f"Prefix : {R2_PREFIX}")
    days, loose = list_level(R2_PREFIX)
    if not days and not loose:
        out("\nNothing found under this prefix - check R2_PREFIX.")
        return
    out(f"\nFolders directly under the prefix: {len(days)}   (files directly under it: {len(loose)})")
    if loose:
        for o in loose[:20]:
            out(f"  file: {name_of(o['Key'])}  ({o['Size'] / 1024:,.1f} KB)")

    summary = []
    folder_days = defaultdict(list)  # folder name -> days it appears in
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for d in days:
            subs, files = list_level(d)
            counts = list(pool.map(count_files, subs))
            out("\n" + "=" * 72)
            out(f"{name_of(d)}   ->   {len(subs)} folder(s), {sum(counts):,} file(s) inside them")
            out("=" * 72)
            for sub, n in zip(subs, counts):
                out(f"  {name_of(sub)}   ({n} file{'s' if n != 1 else ''})")
                folder_days[name_of(sub)].append(name_of(d))
            for o in files[:20]:
                out(f"  [file directly in the day folder] {name_of(o['Key'])}  ({o['Size'] / 1024:,.1f} KB)")
            summary.append((name_of(d), len(subs), sum(counts), len(files)))

    out("\n" + "=" * 72)
    out("SUMMARY")
    out("=" * 72)
    out(f"{'day':<24}{'folders':>10}{'files in folders':>18}{'loose files':>13}")
    for name, n_sub, n_in, n_loose in summary:
        out(f"{name:<24}{n_sub:>10}{n_in:>18,}{n_loose:>13}")
    counts = {n for _, n, _, _ in summary}
    out(f"\nDays: {len(summary)} | folders per day: "
        + (f"{min(counts)}" if len(counts) == 1 else f"{min(counts)} to {max(counts)} (it changes between days)"))

    # folder names that appear in more than one day
    total_days = len(days)
    repeated = {k: v for k, v in folder_days.items() if len(v) > 1}
    single = len(folder_days) - len(repeated)
    out("\n" + "=" * 72)
    out("FOLDERS REPEATED IN MORE THAN ONE DAY")
    out("=" * 72)
    out(f"Distinct folder names: {len(folder_days):,} | in more than one day: {len(repeated):,} "
        f"| in a single day only: {single:,} | in every day ({total_days}): "
        f"{sum(1 for v in folder_days.values() if len(v) == total_days):,}")
    if repeated:
        out("")
        for name, ds in sorted(repeated.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            where = f"all {total_days} days" if len(ds) == total_days else f"{len(ds)} days: " + ", ".join(ds)
            out(f"  {name}   ({where})")
    else:
        out("No folder name appears in more than one day.")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "structure_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LINES))


if __name__ == "__main__":
    sys.exit(main())