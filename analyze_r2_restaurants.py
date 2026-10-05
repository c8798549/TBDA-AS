import os
import json
from collections import defaultdict

import boto3
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

R2_PREFIX = "merged-restaurant-info/year=2025/month=09/day=17/"

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


# Columns used for duplicate-value analysis
DUPLICATION_COLUMNS = [
    "id",
    "name",
    "restaurantSlug",
    "branchId",
    "branchName",
    "branchSlug",
]


# Specific groups for relationship analysis
RELATIONSHIP_GROUPS = [
    ["id", "name", "restaurantSlug"],
    ["branchId", "branchName", "branchSlug"],
    ["id", "branchId", "branchName"],
]


# Columns for global unique-value analysis
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


# Columns for per-file min/max analysis
MIN_MAX_COLUMNS = [
    "deliveryTime",
    "deliveryFee",
]


# Columns for global rating analysis
RATING_COLUMNS = [
    "rate",
    "totalReviews",
    "totalRatings",
]


# ============================================================
# R2 CLIENT
# ============================================================

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)

BUCKET_NAME = os.environ["CF_R2_BUCKET_NAME"]


# ============================================================
# HELPERS
# ============================================================

def normalize_value(value):
    """
    Normalize values before duplicate / relationship analysis.

    Lists and dictionaries are converted to JSON strings
    so they can be compared safely.
    """

    if pd.isna(value) if not isinstance(value, (list, dict)) else False:
        return None

    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False
        )

    if isinstance(value, float):
        if value.is_integer():
            return int(value)

    return value


def format_value(value):
    """
    Format values for readable console output.
    """

    if value is None:
        return "NULL"

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))

    return str(value)


def load_json_file(bucket_name, key):
    """
    Download and load one JSON file from R2.
    """

    response = s3.get_object(
        Bucket=bucket_name,
        Key=key
    )

    content = response["Body"].read().decode("utf-8")

    data = json.loads(content)

    # Handle different possible JSON structures
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # If the actual records are inside a common key
        for key_name in ["data", "records", "items", "results"]:
            if key_name in data and isinstance(data[key_name], list):
                return data[key_name]

        # Otherwise treat the dict itself as one record
        return [data]

    return []


def get_unique_values(series):
    """
    Return normalized unique non-null values from a column.
    """

    values = set()

    for value in series:
        normalized = normalize_value(value)

        if normalized is not None:
            values.add(str(normalized))

    return values


# ============================================================
# DUPLICATION ANALYSIS
# ============================================================

def analyze_group_relationships(df, columns):
    """
    Analyze pairwise relationships inside one relationship group.

    For every pair:
        A -> B
        B -> A

    Only one-to-many relationships are reported.
    Maximum 10 example values are shown for each direction.
    """

    available_columns = [
        column
        for column in columns
        if column in df.columns
    ]

    if len(available_columns) < 2:
        return

    for i, column_a in enumerate(available_columns):

        for column_b in available_columns[i + 1:]:

            temp = df[[column_a, column_b]].copy()

            temp = temp.dropna(
                subset=[column_a, column_b]
            )

            if temp.empty:
                continue

            temp[column_a] = temp[column_a].apply(
                normalize_value
            )

            temp[column_b] = temp[column_b].apply(
                normalize_value
            )

            print(
                f"\n{column_a} <-> {column_b}"
            )

            # ------------------------------------------------
            # A -> B
            # ------------------------------------------------

            mapping_a_to_b = (
                temp.groupby(column_a)[column_b]
                .nunique()
            )

            multiple_a_to_b = (
                mapping_a_to_b[
                    mapping_a_to_b > 1
                ]
            )

            if multiple_a_to_b.empty:

                print(
                    f"  {column_a} -> {column_b}: "
                    "No one-to-many relationship"
                )

            else:

                print(
                    f"  {column_a} -> {column_b}: "
                    f"{len(multiple_a_to_b)} values have "
                    f"multiple {column_b} values"
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

                    print(
                        f"    {column_a} = "
                        f"{format_value(value_a)}"
                    )

                    print(
                        f"      {column_b}: "
                        f"{related_values}"
                    )

            # ------------------------------------------------
            # B -> A
            # ------------------------------------------------

            mapping_b_to_a = (
                temp.groupby(column_b)[column_a]
                .nunique()
            )

            multiple_b_to_a = (
                mapping_b_to_a[
                    mapping_b_to_a > 1
                ]
            )

            if multiple_b_to_a.empty:

                print(
                    f"  {column_b} -> {column_a}: "
                    "No one-to-many relationship"
                )

            else:

                print(
                    f"  {column_b} -> {column_a}: "
                    f"{len(multiple_b_to_a)} values have "
                    f"multiple {column_a} values"
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

                    print(
                        f"    {column_b} = "
                        f"{format_value(value_b)}"
                    )

                    print(
                        f"      {column_a}: "
                        f"{related_values}"
                    )


def analyze_duplication_relationships(df):
    """
    Analyze only the requested relationship groups.
    """

    print("\n" + "-" * 100)
    print("DUPLICATION RELATIONSHIP ANALYSIS")
    print("-" * 100)

    for group_number, columns in enumerate(
        RELATIONSHIP_GROUPS,
        start=1
    ):

        print(
            f"\nGROUP {group_number}: "
            f"{' <-> '.join(columns)}"
        )

        print("-" * 80)

        analyze_group_relationships(
            df,
            columns
        )


def analyze_duplicates(df):
    """
    Analyze duplicate values for the six duplication columns.
    """

    print("\n" + "-" * 100)
    print("DUPLICATE VALUES")
    print("-" * 100)

    found_duplicates = False

    for column in DUPLICATION_COLUMNS:

        if column not in df.columns:

            print(
                f"\n{column}: COLUMN NOT FOUND"
            )

            continue

        normalized = df[column].apply(
            normalize_value
        )

        counts = normalized.value_counts(
            dropna=True
        )

        duplicates = counts[
            counts > 1
        ]

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

    # Relationship analysis
    analyze_duplication_relationships(df)

    return found_duplicates


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 100)
    print("R2 RESTAURANT DATA ANALYSIS")
    print("=" * 100)

    print(f"\nBucket : {BUCKET_NAME}")
    print(f"Prefix : {R2_PREFIX}")

    # --------------------------------------------------------
    # List files
    # --------------------------------------------------------

    files = []

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    for page in paginator.paginate(
        Bucket=BUCKET_NAME,
        Prefix=R2_PREFIX
    ):

        for obj in page.get("Contents", []):

            key = obj["Key"]

            if key.lower().endswith(
                ".json"
            ):
                files.append(key)

    files.sort()

    print(
        f"\nTotal JSON files found: "
        f"{len(files)}"
    )

    if not files:
        print(
            "\nNo JSON files found."
        )
        return

    # --------------------------------------------------------
    # Global containers
    # --------------------------------------------------------

    global_unique_values = defaultdict(set)

    global_rating_data = []

    processed_files = []

    # Column completeness tracking
    column_stats = {
        column: {
            "files_present": 0,
            "files_missing": 0,
            "total_records": 0,
            "non_null_records": 0,
            "null_records": 0,
        }
        for column in COLUMN_ORDER
    }

    # --------------------------------------------------------
    # Process every file
    # --------------------------------------------------------

    for file_number, file_key in enumerate(
        files,
        start=1
    ):

        print("\n\n")
        print("=" * 100)
        print(
            f"FILE {file_number}/{len(files)}"
        )
        print("=" * 100)

        print(
            f"\nPath: {file_key}"
        )

        try:

            records = load_json_file(
                BUCKET_NAME,
                file_key
            )

            if not records:

                print(
                    "\nNo records found."
                )

                processed_files.append(
                    file_key
                )

                continue

            df = pd.DataFrame(records)

            print(
                f"\nRecords: {len(df)}"
            )

            print(
                f"Columns found: {len(df.columns)}"
            )

            processed_files.append(
                file_key
            )

            # ------------------------------------------------
            # Column completeness
            # ------------------------------------------------

            for column in COLUMN_ORDER:

                stats = column_stats[column]

                stats["total_records"] += len(df)

                if column not in df.columns:

                    stats["files_missing"] += 1

                    continue

                stats["files_present"] += 1

                null_mask = df[column].isna()

                null_count = int(
                    null_mask.sum()
                )

                non_null_count = (
                    len(df) - null_count
                )

                stats["null_records"] += (
                    null_count
                )

                stats["non_null_records"] += (
                    non_null_count
                )

            # ------------------------------------------------
            # Global unique values
            # ------------------------------------------------

            for column in UNIQUE_COLUMNS:

                if column not in df.columns:
                    continue

                values = get_unique_values(
                    df[column]
                )

                global_unique_values[
                    column
                ].update(values)

            # ------------------------------------------------
            # Per-file min / max
            # ------------------------------------------------

            print("\n")
            print("-" * 100)
            print("MIN / MAX")
            print("-" * 100)

            for column in MIN_MAX_COLUMNS:

                if column not in df.columns:

                    print(
                        f"\n{column}: "
                        "COLUMN NOT FOUND"
                    )

                    continue

                numeric_values = pd.to_numeric(
                    df[column],
                    errors="coerce"
                ).dropna()

                if numeric_values.empty:

                    print(
                        f"\n{column}: "
                        "No numeric values"
                    )

                    continue

                print(
                    f"\n{column}:"
                )

                print(
                    f"  Min: "
                    f"{numeric_values.min()}"
                )

                print(
                    f"  Max: "
                    f"{numeric_values.max()}"
                )

            # ------------------------------------------------
            # Duplicate analysis
            # ------------------------------------------------

            analyze_duplicates(df)

            # ------------------------------------------------
            # Rating data
            # ------------------------------------------------

            if all(
                column in df.columns
                for column in RATING_COLUMNS
            ):

                rating_df = df[
                    RATING_COLUMNS
                ].copy()

                for column in RATING_COLUMNS:

                    rating_df[column] = (
                        pd.to_numeric(
                            rating_df[column],
                            errors="coerce"
                        )
                    )

                global_rating_data.append(
                    rating_df
                )

        except Exception as e:

            print(
                f"\nERROR processing file:"
            )

            print(
                f"  {e}"
            )

    # ========================================================
    # GLOBAL UNIQUE VALUES
    # ========================================================

    print("\n\n")
    print("#" * 100)
    print("GLOBAL UNIQUE VALUES — ALL FILES")
    print("#" * 100)

    print(
        f"\nTotal files found: "
        f"{len(files)}"
    )

    print(
        f"Successfully processed: "
        f"{len(processed_files)}"
    )

    for column in UNIQUE_COLUMNS:

        values = sorted(
            global_unique_values[column]
        )

        print("\n" + "-" * 100)

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

    # ========================================================
    # GLOBAL RATING ANALYSIS
    # ========================================================

    print("\n\n")
    print("#" * 100)
    print("GLOBAL RATING ANALYSIS — ALL FILES")
    print("#" * 100)

    if global_rating_data:

        all_ratings = pd.concat(
            global_rating_data,
            ignore_index=True
        )

        # ----------------------------------------------------
        # Basic statistics
        # ----------------------------------------------------

        print("\nBasic statistics:")

        for column in RATING_COLUMNS:

            values = all_ratings[
                column
            ].dropna()

            print(
                f"\n{column}:"
            )

            if values.empty:

                print(
                    "  Min    : No values"
                )

                print(
                    "  Max    : No values"
                )

                print(
                    "  Unique : 0"
                )

                continue

            print(
                f"  Min    : "
                f"{values.min()}"
            )

            print(
                f"  Max    : "
                f"{values.max()}"
            )

            print(
                f"  Unique : "
                f"{values.nunique()}"
            )

        # ----------------------------------------------------
        # Comparison
        # ----------------------------------------------------

        comparison_df = all_ratings.dropna(
            subset=RATING_COLUMNS
        )

        equal_count = (
            comparison_df["totalReviews"]
            == comparison_df["totalRatings"]
        ).sum()

        reviews_greater = (
            comparison_df["totalReviews"]
            > comparison_df["totalRatings"]
        ).sum()

        ratings_greater = (
            comparison_df["totalRatings"]
            > comparison_df["totalReviews"]
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

    else:

        print(
            "\nNo valid rating data found."
        )

    # ========================================================
    # COLUMN COMPLETENESS
    # ========================================================

    print("\n\n")
    print("#" * 100)
    print("COLUMN COMPLETENESS — ALL FILES")
    print("#" * 100)

    total_files = len(files)

    for column in COLUMN_ORDER:

        stats = column_stats[column]

        files_present = stats[
            "files_present"
        ]

        files_missing = stats[
            "files_missing"
        ]

        total_records = stats[
            "total_records"
        ]

        non_null_records = stats[
            "non_null_records"
        ]

        null_records = stats[
            "null_records"
        ]

        # ----------------------------------------------------
        # Determine status
        # ----------------------------------------------------

        if files_present == 0:

            status = "MISSING FROM ALL FILES"

        elif files_missing > 0:

            if null_records > 0:

                status = (
                    "MISSING FROM SOME FILES "
                    "+ HAS NULLS"
                )

            else:

                status = (
                    "MISSING FROM SOME FILES"
                )

        else:

            if null_records == 0:

                status = "NEVER NULL"

            else:

                status = "SOMETIMES NULL"

        # ----------------------------------------------------
        # Null percentage
        # ----------------------------------------------------

        if total_records > 0:

            null_percentage = (
                null_records
                / total_records
                * 100
            )

        else:

            null_percentage = 0

        print("\n" + "-" * 100)

        print(
            f"{column}"
        )

        print(
            f"  Status          : "
            f"{status}"
        )

        print(
            f"  Files Present   : "
            f"{files_present}/{total_files}"
        )

        print(
            f"  Files Missing   : "
            f"{files_missing}/{total_files}"
        )

        print(
            f"  Total Records   : "
            f"{total_records}"
        )

        print(
            f"  Non-null        : "
            f"{non_null_records}"
        )

        print(
            f"  Null            : "
            f"{null_records}"
        )

        print(
            f"  Null %          : "
            f"{null_percentage:.2f}%"
        )

    # ========================================================
    # FINISHED
    # ========================================================

    print("\n\n")
    print("=" * 100)
    print("ANALYSIS COMPLETED")
    print("=" * 100)

    print(
        f"\nTotal files found     : "
        f"{len(files)}"
    )

    print(
        f"Successfully processed: "
        f"{len(processed_files)}"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()