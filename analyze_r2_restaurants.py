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


# ============================================================
# Columns expected in the files
# ============================================================

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
# Columns to check for duplication
# ============================================================

DUPLICATION_COLUMNS = [
    "id",
    "name",
    "restaurantSlug",
    "branchId",
    "branchName",
    "branchSlug",
]


# ============================================================
# Columns whose unique values will be collected
# and printed ONLY ONCE at the end
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
# Columns for min / max
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
# Helper Functions
# ============================================================

def normalize_value(value):
    """
    Convert lists/dictionaries into strings so they can
    safely be compared and stored in sets.
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
    Format a value for printing.
    """

    if value is None:
        return "NULL"

    return str(value)


def load_json_file(key):
    """
    Load a JSON file from R2.

    Supports:
    - JSON array
    - Single JSON object
    - JSONL / NDJSON
    """

    response = s3.get_object(
        Bucket=BUCKET_NAME,
        Key=key
    )

    content = response["Body"].read().decode("utf-8").strip()

    if not content:
        return []

    # --------------------------------------------------------
    # Try normal JSON
    # --------------------------------------------------------

    try:

        data = json.loads(content)

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return [data]

    except json.JSONDecodeError:
        pass


    # --------------------------------------------------------
    # Try JSONL
    # --------------------------------------------------------

    records = []

    for line_number, line in enumerate(
        content.splitlines(),
        start=1
    ):

        line = line.strip()

        if not line:
            continue

        try:

            records.append(
                json.loads(line)
            )

        except json.JSONDecodeError as e:

            print(
                f"WARNING: Could not parse "
                f"line {line_number} in {key}: {e}"
            )

    return records


def get_unique_values(series):
    """
    Get unique non-null values safely.
    Handles lists and dictionaries.
    """

    values = set()

    for value in series:

        value = normalize_value(value)

        if value is not None:

            values.add(
                str(value)
            )

    return values


# ============================================================
# Rating / Review / Ratings Analysis
# ============================================================

def analyze_rating_relationship(df, file_name):

    print("\n" + "-" * 100)
    print("RATING / REVIEW RELATIONSHIP")
    print("-" * 100)

    required_columns = [
        "rate",
        "totalReviews",
        "totalRatings",
    ]

    missing = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing:

        print(
            f"Missing columns: {missing}"
        )

        return


    rating_df = df[
        required_columns
    ].copy()


    # --------------------------------------------------------
    # Convert to numeric
    # --------------------------------------------------------

    for column in required_columns:

        rating_df[column] = pd.to_numeric(
            rating_df[column],
            errors="coerce"
        )


    # Keep only rows where all three values exist
    rating_df = rating_df.dropna(
        subset=required_columns
    )


    if rating_df.empty:

        print(
            "No valid numeric records found."
        )

        return


    # --------------------------------------------------------
    # Basic statistics
    # --------------------------------------------------------

    print("\nBasic statistics:")

    for column in required_columns:

        print(
            f"\n{column}:"
        )

        print(
            f"  Min    : "
            f"{rating_df[column].min()}"
        )

        print(
            f"  Max    : "
            f"{rating_df[column].max()}"
        )

        print(
            f"  Unique : "
            f"{rating_df[column].nunique()}"
        )


    # --------------------------------------------------------
    # Compare totalReviews vs totalRatings
    # --------------------------------------------------------

    equal_count = (
        rating_df["totalReviews"]
        == rating_df["totalRatings"]
    ).sum()

    reviews_greater = (
        rating_df["totalReviews"]
        > rating_df["totalRatings"]
    ).sum()

    ratings_greater = (
        rating_df["totalRatings"]
        > rating_df["totalReviews"]
    ).sum()


    print("\nComparison:")

    print(
        f"  totalReviews == totalRatings : "
        f"{equal_count}"
    )

    print(
        f"  totalReviews > totalRatings  : "
        f"{reviews_greater}"
    )

    print(
        f"  totalRatings > totalReviews  : "
        f"{ratings_greater}"
    )


    # --------------------------------------------------------
    # Correlation
    # --------------------------------------------------------

    print("\nCorrelation:")

    correlation = rating_df[
        [
            "rate",
            "totalReviews",
            "totalRatings",
        ]
    ].corr()

    print(
        correlation.to_string()
    )


    # --------------------------------------------------------
    # Records where Reviews != Ratings
    # --------------------------------------------------------

    different = rating_df[
        rating_df["totalReviews"]
        != rating_df["totalRatings"]
    ]

    print(
        f"\nRecords where "
        f"totalReviews != totalRatings: "
        f"{len(different)}"
    )


    if not different.empty:

        print(
            "\nSample of records where "
            "they are different:"
        )

        print(
            different[
                [
                    "rate",
                    "totalReviews",
                    "totalRatings",
                ]
            ]
            .head(20)
            .to_string(index=False)
        )


    # --------------------------------------------------------
    # Highest totalRatings values
    # --------------------------------------------------------

    print(
        "\nTop totalRatings values:"
    )

    print(
        rating_df[
            [
                "rate",
                "totalReviews",
                "totalRatings",
            ]
        ]
        .sort_values(
            "totalRatings",
            ascending=False
        )
        .head(20)
        .to_string(index=False)
    )


    # --------------------------------------------------------
    # Highest totalReviews values
    # --------------------------------------------------------

    print(
        "\nTop totalReviews values:"
    )

    print(
        rating_df[
            [
                "rate",
                "totalReviews",
                "totalRatings",
            ]
        ]
        .sort_values(
            "totalReviews",
            ascending=False
        )
        .head(20)
        .to_string(index=False)
    )


# ============================================================
# Get JSON files from R2
# ============================================================

print("\n" + "=" * 100)
print("R2 RESTAURANT DATA ANALYSIS")
print("=" * 100)

print(
    f"Bucket : {BUCKET_NAME}"
)

print(
    f"Prefix : {PREFIX}"
)

print("\nFetching files...")


files = []

paginator = s3.get_paginator(
    "list_objects_v2"
)


for page in paginator.paginate(
    Bucket=BUCKET_NAME,
    Prefix=PREFIX
):

    for obj in page.get(
        "Contents",
        []
    ):

        key = obj["Key"]

        if key.lower().endswith(".json"):

            files.append(key)


files = sorted(files)


print(
    f"\nJSON files found: "
    f"{len(files)}"
)


if not files:

    raise RuntimeError(
        f"No JSON files found under {PREFIX}"
    )


# ============================================================
# Global unique values
# ============================================================

global_unique_values = defaultdict(set)


# ============================================================
# Column completeness tracking
# ============================================================

column_stats = defaultdict(
    lambda: {
        "files_present": 0,
        "files_missing": 0,
        "total_records": 0,
        "null_records": 0,
        "non_null_records": 0,
    }
)


# ============================================================
# Process each file
# ============================================================

for file_number, key in enumerate(
    files,
    start=1
):

    file_name = key.split("/")[-1]


    print("\n\n")
    print("=" * 100)

    print(
        f"FILE {file_number}/{len(files)}"
    )

    print(file_name)

    print("=" * 100)


    # --------------------------------------------------------
    # Load file
    # --------------------------------------------------------

    try:

        records = load_json_file(key)

    except ClientError as e:

        print(
            f"ERROR reading {key}"
        )

        print(e)

        continue

    except Exception as e:

        print(
            f"ERROR processing {key}"
        )

        print(e)

        continue


    # --------------------------------------------------------
    # DataFrame
    # --------------------------------------------------------

    df = pd.DataFrame(records)


    # --------------------------------------------------------
    # Number of records
    # --------------------------------------------------------

    print(
        f"\nNumber of records: "
        f"{len(df)}"
    )


    # ========================================================
    # Column Completeness Tracking
    # ========================================================

    for column in COLUMN_ORDER:

        stats = column_stats[column]


        if column not in df.columns:

            stats["files_missing"] += 1

            continue


        stats["files_present"] += 1

        stats["total_records"] += len(df)


        null_mask = df[column].isna()

        null_count = null_mask.sum()

        non_null_count = (
            len(df) - null_count
        )


        stats["null_records"] += (
            null_count
        )

        stats["non_null_records"] += (
            non_null_count
        )


    # ========================================================
    # Duplication Analysis
    # ========================================================
    def analyze_duplication_relationships(df):
        print("\n" + "-" * 100)
        print("DUPLICATION RELATIONSHIP ANALYSIS")
        print("-" * 100)

        available_columns = [
            column for column in DUPLICATION_COLUMNS
            if column in df.columns
        ]

        if len(available_columns) < 2:
            print("\nNot enough columns available for relationship analysis.")
            return

        found_relationships = False

        # Analyze every pair in both directions
        for i, column_a in enumerate(available_columns):
            for column_b in available_columns[i + 1:]:
                
                temp = df[[column_a, column_b]].copy()

                # Ignore rows where either side is NULL
                temp = temp.dropna(subset=[column_a, column_b])

                if temp.empty:
                    continue

                # Normalize values so lists/dicts can be compared safely
                temp[column_a] = temp[column_a].apply(normalize_value)
                temp[column_b] = temp[column_b].apply(normalize_value)

                print(f"\n{column_a} <-> {column_b}")

                # ---------------------------------------------------------
                # column_a -> column_b
                # ---------------------------------------------------------
                mapping_a_to_b = (
                    temp.groupby(column_a, dropna=True)[column_b]
                    .nunique()
                )

                multiple_a_to_b = mapping_a_to_b[
                    mapping_a_to_b > 1
                ]

                if not multiple_a_to_b.empty:
                    found_relationships = True

                    print(
                        f"\n  {column_a} -> {column_b}: "
                        f"{len(multiple_a_to_b)} values have multiple "
                        f"{column_b} values"
                    )

                    for value_a in multiple_a_to_b.index[:10]:
                        related_values = (
                            temp.loc[
                                temp[column_a] == value_a,
                                column_b
                            ]
                            .drop_duplicates()
                            .tolist()
                        )

                        print(f"    {column_a} = {value_a}")
                        print(f"      {column_b} values: {related_values}")

                else:
                    print(
                        f"\n  {column_a} -> {column_b}: "
                        f"No one-to-many relationship found"
                    )

                # ---------------------------------------------------------
                # column_b -> column_a
                # ---------------------------------------------------------
                mapping_b_to_a = (
                    temp.groupby(column_b, dropna=True)[column_a]
                    .nunique()
                )

                multiple_b_to_a = mapping_b_to_a[
                    mapping_b_to_a > 1
                ]

                if not multiple_b_to_a.empty:
                    found_relationships = True

                    print(
                        f"\n  {column_b} -> {column_a}: "
                        f"{len(multiple_b_to_a)} values have multiple "
                        f"{column_a} values"
                    )

                    for value_b in multiple_b_to_a.index[:10]:
                        related_values = (
                            temp.loc[
                                temp[column_b] == value_b,
                                column_a
                            ]
                            .drop_duplicates()
                            .tolist()
                        )

                        print(f"    {column_b} = {value_b}")
                        print(f"      {column_a} values: {related_values}")

                else:
                    print(
                        f"\n  {column_b} -> {column_a}: "
                        f"No one-to-many relationship found"
                    )

        if not found_relationships:
            print(
                "\nNo one-to-many relationships found between "
                "the duplication columns."
            )


    print("\n" + "-" * 100)
    print("DUPLICATION ANALYSIS")
    print("-" * 100)

    found_duplicates = False

    for column in DUPLICATION_COLUMNS:
        if column not in df.columns:
            print(f"\n{column}: COLUMN NOT FOUND")
            continue

        normalized = df[column].apply(normalize_value)
        counts = normalized.value_counts(dropna=True)
        duplicates = counts[counts > 1]

        if duplicates.empty:
            print(f"\n{column}: No duplicates")
        else:
            found_duplicates = True

            print(f"\n{column}: {len(duplicates)} duplicated values")

            for value, count in duplicates.items():
                print(f"  {format_value(value)} -> {count} times")

    if not found_duplicates:
        print("\nNo duplicates found in the checked columns.")

    # Analyze relationships between the duplication columns
    analyze_duplication_relationships(df)


    # ========================================================
    # Collect Global Unique Values
    #
    # They are NOT printed here.
    # ========================================================

    for column in UNIQUE_COLUMNS:

        if column not in df.columns:

            continue


        values = get_unique_values(
            df[column]
        )


        global_unique_values[
            column
        ].update(values)


    # ========================================================
    # Min / Max
    # ========================================================

    print("\n" + "-" * 100)
    print("MIN / MAX")
    print("-" * 100)


    for column in MIN_MAX_COLUMNS:

        if column not in df.columns:

            print(
                f"\n{column}: "
                f"COLUMN NOT FOUND"
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


        print(
            f"\n{column}"
        )

        print(
            f"  Min: "
            f"{numeric_values.min()}"
        )

        print(
            f"  Max: "
            f"{numeric_values.max()}"
        )


    # ========================================================
    # Rating / Review Relationship
    # ========================================================

    analyze_rating_relationship(
        df,
        file_name
    )


# ============================================================
# GLOBAL UNIQUE VALUES
# ============================================================

print("\n\n")

print("#" * 100)
print("GLOBAL UNIQUE VALUES — ALL FILES")
print("#" * 100)

print(
    f"\nTotal files analyzed: "
    f"{len(files)}"
)


for column in UNIQUE_COLUMNS:

    values = sorted(
        global_unique_values[column]
    )


    print(
        "\n" + "-" * 100
    )


    print(
        f"{column} "
        f"({len(values)} unique values "
        f"across ALL files):"
    )


    if not values:

        print(
            "  [No non-null values]"
        )

    else:

        for value in values:

            print(
                f"  - {value}"
            )


# ============================================================
# COLUMN COMPLETENESS SUMMARY
# ============================================================

print("\n\n")

print("#" * 100)
print("COLUMN COMPLETENESS SUMMARY — ALL FILES")
print("#" * 100)


total_files = len(files)


for column in COLUMN_ORDER:

    stats = column_stats[column]


    files_present = stats["files_present"]

    files_missing = stats["files_missing"]

    total_records = stats["total_records"]

    null_records = stats["null_records"]

    non_null_records = stats["non_null_records"]


    # --------------------------------------------------------
    # Determine status
    # --------------------------------------------------------

    if files_present == 0:

        status = "MISSING FROM ALL FILES"

    elif files_missing > 0:

        if null_records == 0:

            status = "MISSING FROM SOME FILES"

        else:

            status = (
                "MISSING FROM SOME FILES + HAS NULLS"
            )

    elif null_records == 0:

        status = "NEVER NULL"

    elif non_null_records == 0:

        status = "ALWAYS NULL"

    else:

        status = "SOMETIMES NULL"


    # --------------------------------------------------------
    # Null percentage
    # --------------------------------------------------------

    if total_records > 0:

        null_percentage = (
            null_records
            / total_records
        ) * 100

    else:

        null_percentage = 0


    # --------------------------------------------------------
    # Print
    # --------------------------------------------------------

    print(
        "\n" + "-" * 100
    )

    print(
        f"Column: {column}"
    )

    print(
        f"Status: {status}"
    )

    print(
        f"Files present: "
        f"{files_present}/{total_files}"
    )

    print(
        f"Files missing: "
        f"{files_missing}/{total_files}"
    )

    print(
        f"Total records: "
        f"{total_records}"
    )

    print(
        f"Non-null records: "
        f"{non_null_records}"
    )

    print(
        f"Null records: "
        f"{null_records}"
    )

    print(
        f"Null percentage: "
        f"{null_percentage:.2f}%"
    )


# ============================================================
# Complete
# ============================================================

print("\n")

print("#" * 100)
print("ANALYSIS COMPLETE")
print("#" * 100)