import os
import json
import boto3
import pandas as pd
from collections import defaultdict
from botocore.exceptions import ClientError


# ============================================================
# Configuration
# ============================================================

BUCKET_NAME = os.environ["CF_R2_BUCKET_NAME"]
ENDPOINT_URL = os.environ["CF_R2_ENDPOINT_URL"]
ACCESS_KEY_ID = os.environ["CF_R2_ACCESS_KEY_ID"]
SECRET_ACCESS_KEY = os.environ["CF_R2_SECRET_ACCESS_KEY"]

PREFIX = "merged-restaurant-info/year=2025/month=09/day=17/"

# Columns expected in the files
COLUMN_ORDER = [
    "id",
    "name",
    "restaurantId",
    "restaurantSlug",
    "branchId",
    "branchName",
    "branchSlug",
    "branchUrl",
    "areaName",
    "areaId",
    "deliveryAreaId",
    "shopArea",
    "shopCity",
    "latitude",
    "longitude",
    "cuisineString",
    "cuisines",
    "verticalType",
    "isGrocery",
    "shopType",
    "isDarkstore",
    "isCokeRestaurant",
    "rate",
    "totalReviews",
    "totalRatings",
    "deliveryFee",
    "minimumOrderAmount",
    "avgDeliveryTime",
    "deliveryTime",
    "deliveryChargesType",
    "deliverySchedule",
    "preOrder",
    "isTalabatGO",
    "statusCode",
    "status",
    "isNew",
    "IsMigratedToDh",
    "promotionText",
    "discountText",
    "isShopSponcered",
    "Sponsored",
    "shopPosition",
    "acceptCreditCard",
    "acceptDebitCard",
    "acceptCash",
    "availablePaymentMethods",
    "contactlessDelivery",
    "IsProvideTracking",
    "isProvideOrderStatus",
    "isCateringAvailable",
    "grlRequired",
    "heroImage",
    "logo",
    "summary",
    "altTalabattxt",
    "altMunicipaltxt",
    "altTouristtxt",
    "alternativeDeliveryText",
    "branchLearnMoreLink",
    "isVatInclusive",
    "filtersIds",
    "view",
    "page",
    "createdAt",
    "menuUrl",
]


# ============================================================
# Columns where we need duplication analysis
# ============================================================

DUPLICATION_COLUMNS = [
    "id",
    "name",
    "restaurantId",
    "restaurantSlug",
    "branchId",
    "branchName",
    "branchSlug",
    "branchUrl",
]


# ============================================================
# Columns where we need unique values
# Per file + global across all files
# ============================================================

UNIQUE_COLUMNS = [
    "areaName",
    "areaId",
    "deliveryAreaId",
    "shopArea",
    "shopCity",
    "verticalType",
    "isGrocery",
    "shopType",
    "isDarkstore",
    "isCokeRestaurant",
    "minimumOrderAmount",
    "deliveryChargesType",
    "deliverySchedule",
    "preOrder",
    "isTalabatGO",
    "statusCode",
    "status",
    "isNew",
    "IsMigratedToDh",
    "promotionText",
    "discountText",
    "isShopSponcered",
    "acceptCreditCard",
    "acceptDebitCard",
    "acceptCash",
    "contactlessDelivery",
    "IsProvideTracking",
    "isProvideOrderStatus",
    "isCateringAvailable",
    "grlRequired",    
    "summary",
    "altTalabattxt",
    "altMunicipaltxt",
    "altTouristtxt",
    "alternativeDeliveryText",
    "branchLearnMoreLink",
    "isVatInclusive",
    "view"
]


# ============================================================
# Numeric columns for min / max
# ============================================================

MIN_MAX_COLUMNS = [
    "deliveryTime",
    "deliveryFee",
]


# ============================================================
# Connect to Cloudflare R2
# ============================================================

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
    region_name="auto",
)


# ============================================================
# Helper functions
# ============================================================

def normalize_value(value):
    """
    Convert values to something that can safely be used
    for unique-value calculations.
    """

    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True
        )

    return value


def format_value(value):
    """
    Pretty representation for printing values.
    """

    if value is None:
        return "NULL"

    if isinstance(value, str):
        return value

    return str(value)


def load_json_file(key):
    """
    Download and load one JSON file from R2.

    Supports:
    - JSON array
    - Single JSON object
    - JSONL / NDJSON
    """

    response = s3.get_object(
        Bucket=BUCKET_NAME,
        Key=key
    )

    content = response["Body"].read().decode("utf-8")

    content = content.strip()

    if not content:
        return []

    # Try normal JSON first
    try:
        data = json.loads(content)

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return [data]

    except json.JSONDecodeError:
        pass

    # If normal JSON failed, try JSONL
    records = []

    for line_number, line in enumerate(content.splitlines(), start=1):

        line = line.strip()

        if not line:
            continue

        try:
            records.append(json.loads(line))

        except json.JSONDecodeError as e:
            print(
                f"WARNING: Could not parse line {line_number} "
                f"in {key}: {e}"
            )

    return records


def get_unique_values(series):
    """
    Return unique values safely, including lists/dicts.
    """

    values = set()

    for value in series:

        value = normalize_value(value)

        if value is not None:
            values.add(str(value))

    return sorted(values)


# ============================================================
# Get files from R2
# ============================================================

print("\n" + "=" * 100)
print("R2 FILE ANALYSIS")
print("=" * 100)

print(f"Bucket : {BUCKET_NAME}")
print(f"Prefix : {PREFIX}")

print("\nFetching files...")

files = []

paginator = s3.get_paginator("list_objects_v2")

for page in paginator.paginate(
    Bucket=BUCKET_NAME,
    Prefix=PREFIX
):
    for obj in page.get("Contents", []):

        key = obj["Key"]

        if key.lower().endswith(".json"):
            files.append(key)


files = sorted(files)

print(f"\nJSON files found: {len(files)}")

if not files:
    raise RuntimeError(
        f"No JSON files found under {PREFIX}"
    )


# ============================================================
# Global unique values
# ============================================================

global_unique_values = defaultdict(set)


# ============================================================
# Process each file
# ============================================================

for file_number, key in enumerate(files, start=1):

    file_name = key.split("/")[-1]

    print("\n\n")
    print("=" * 100)
    print(f"FILE {file_number}/{len(files)}")
    print(file_name)
    print("=" * 100)

    try:
        records = load_json_file(key)

    except ClientError as e:

        print(f"ERROR reading {key}")
        print(e)

        continue

    except Exception as e:

        print(f"ERROR processing {key}")
        print(e)

        continue


    # --------------------------------------------------------
    # Create DataFrame
    # --------------------------------------------------------

    df = pd.DataFrame(records)

    print(f"\nNumber of records: {len(df)}")


    # --------------------------------------------------------
    # Check columns
    # --------------------------------------------------------

    missing_columns = [
        column
        for column in COLUMN_ORDER
        if column not in df.columns
    ]

    extra_columns = [
        column
        for column in df.columns
        if column not in COLUMN_ORDER
    ]

    if missing_columns:

        print("\nMissing columns:")

        for column in missing_columns:
            print(f"  - {column}")

    if extra_columns:

        print("\nExtra columns:")

        for column in extra_columns:
            print(f"  - {column}")


    # ========================================================
    # 1. UNIQUE COUNT FOR EVERY COLUMN
    # ========================================================

    print("\n" + "-" * 100)
    print("UNIQUE COUNT PER COLUMN")
    print("-" * 100)

    for column in COLUMN_ORDER:

        if column not in df.columns:

            print(f"{column}: COLUMN NOT FOUND")
            continue

        unique_count = df[column].apply(
            normalize_value
        ).nunique(dropna=True)

        print(
            f"{column}: "
            f"{unique_count} unique values"
        )


    # ========================================================
    # 2. DUPLICATION ANALYSIS
    # ========================================================

    print("\n" + "-" * 100)
    print("DUPLICATION ANALYSIS")
    print("-" * 100)

    found_duplicates = False

    for column in DUPLICATION_COLUMNS:

        if column not in df.columns:

            print(f"\n{column}: COLUMN NOT FOUND")
            continue

        normalized = df[column].apply(
            normalize_value
        )

        counts = normalized.value_counts(
            dropna=True
        )

        duplicates = counts[counts > 1]

        if duplicates.empty:

            print(
                f"\n{column}: No duplicates"
            )

        else:

            found_duplicates = True

            print(
                f"\n{column}: "
                f"{len(duplicates)} duplicated values"
            )

            for value, count in duplicates.items():

                print(
                    f"  {format_value(value)} "
                    f"-> {count} times"
                )

    if not found_duplicates:

        print("\nNo duplicates found in the checked columns.")


    # ========================================================
    # 3. COLLECT UNIQUE VALUES PER FILE
    #    Do NOT print them here
    # ========================================================

    for column in UNIQUE_COLUMNS:

        if column not in df.columns:
            continue

        values = get_unique_values(
            df[column]
        )

        # Add this file's unique values
        # to the global collection
        global_unique_values[column].update(values)


    # ========================================================
    # 4. MIN / MAX
    # ========================================================

    print("\n" + "-" * 100)
    print("MIN / MAX")
    print("-" * 100)

    for column in MIN_MAX_COLUMNS:

        if column not in df.columns:

            print(
                f"\n{column}: COLUMN NOT FOUND"
            )

            continue

        numeric_values = pd.to_numeric(
            df[column],
            errors="coerce"
        ).dropna()

        if numeric_values.empty:

            print(
                f"\n{column}: "
                f"No numeric values"
            )

            continue

        print(f"\n{column}")

        print(
            f"  Min: {numeric_values.min()}"
        )

        print(
            f"  Max: {numeric_values.max()}"
        )


# ============================================================
# GLOBAL UNIQUE VALUES
# ============================================================

print("\n\n")
print("#" * 100)
print("GLOBAL UNIQUE VALUES — ALL FILES")
print("#" * 100)

print(
    f"\nTotal files analyzed: {len(files)}"
)

for column in UNIQUE_COLUMNS:

    values = sorted(
        global_unique_values[column]
    )

    print("\n" + "-" * 100)

    print(
        f"{column} "
        f"({len(values)} unique values across ALL files):"
    )

    if not values:

        print("  [No non-null values]")

    else:

        for value in values:

            print(f"  - {value}")


print("\n")
print("#" * 100)
print("ANALYSIS COMPLETE")
print("#" * 100)