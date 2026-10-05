import os
import json
from collections import defaultdict

import boto3
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

R2_PREFIX = "merged-restaurant-info/year=2025/month=09/day=17/"

OUTPUT_FILE = "restaurant_analysis_2025-09-17.json"


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
# DUPLICATION ANALYSIS
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
# RELATIONSHIP ANALYSIS
# ============================================================

RELATIONSHIP_GROUPS = [
    ["id", "name", "restaurantSlug"],
    ["branchId", "branchName", "branchSlug"],
    ["id", "branchId", "branchName"],
]


# ============================================================
# GLOBAL UNIQUE VALUES
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
# MIN / MAX
# ============================================================

MIN_MAX_COLUMNS = [
    "deliveryTime",
    "deliveryFee",
]


# ============================================================
# RATING ANALYSIS
# ============================================================

RATING_COLUMNS = [
    "rate",
    "totalReviews",
    "totalRatings",
]


# ============================================================
# SPECIAL VALUES
# ============================================================

NULL_MARKER = "__R2_ANALYSIS_NULL__"
EMPTY_MARKER = "__R2_ANALYSIS_EMPTY__"


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
# VALUE HELPERS
# ============================================================

def is_null_value(value):
    if value is None:
        return True

    if isinstance(value, (list, dict)):
        return False

    try:
        result = pd.isna(value)

        if isinstance(result, bool):
            return result

        return False

    except (TypeError, ValueError):
        return False


def is_empty_value(value):
    if not isinstance(value, str):
        return False

    return value.strip() == ""


def is_actual_value(value):
    return (
        not is_null_value(value)
        and not is_empty_value(value)
    )


def normalize_value(value):
    """
    Keep NULL and EMPTY as different values.
    """

    if is_null_value(value):
        return NULL_MARKER

    if is_empty_value(value):
        return EMPTY_MARKER

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
    Convert internal markers to readable JSON/output values.
    """

    if value == NULL_MARKER:
        return "NULL"

    if value == EMPTY_MARKER:
        return "EMPTY"

    if value is None:
        return "NULL"

    if isinstance(value, float):

        if value.is_integer():
            return str(int(value))

    return str(value)


def json_safe_value(value):
    """
    Convert values to JSON-safe readable values.
    """

    if value == NULL_MARKER:
        return "NULL"

    if value == EMPTY_MARKER:
        return "EMPTY"

    if value is None:
        return "NULL"

    if isinstance(value, (dict, list)):
        return value

    if hasattr(value, "item"):

        try:
            return value.item()
        except Exception:
            pass

    return value


# ============================================================
# LOAD JSON
# ============================================================

def load_json_file(bucket_name, key):

    response = s3.get_object(
        Bucket=bucket_name,
        Key=key
    )

    content = response["Body"].read().decode("utf-8")

    data = json.loads(content)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for key_name in [
            "data",
            "records",
            "items",
            "results"
        ]:

            if (
                key_name in data
                and isinstance(data[key_name], list)
            ):
                return data[key_name]

        return [data]

    return []


# ============================================================
# UNIQUE VALUES
# ============================================================

def get_unique_values(series):

    values = set()

    for value in series:

        if not is_actual_value(value):
            continue

        normalized = normalize_value(value)

        values.add(str(normalized))

    return values


# ============================================================
# DUPLICATES
# ============================================================

def analyze_duplicates(df):

    results = {}

    print("\n" + "-" * 100)
    print("DUPLICATE VALUES")
    print("-" * 100)

    for column in DUPLICATION_COLUMNS:

        if column not in df.columns:

            results[column] = {
                "status": "COLUMN NOT FOUND",
                "duplicated_values": {}
            }

            print(
                f"\n{column}: COLUMN NOT FOUND"
            )

            continue

        normalized = df[column].apply(
            normalize_value
        )

        counts = normalized.value_counts(
            dropna=False
        )

        duplicates = counts[
            counts > 1
        ]

        duplicated_values = {}

        for value, count in duplicates.items():

            formatted_value = format_value(value)

            duplicated_values[
                formatted_value
            ] = int(count)

        if duplicates.empty:

            results[column] = {
                "status": "NO DUPLICATES",
                "duplicated_values": {}
            }

            print(
                f"\n{column}: No duplicates"
            )

        else:

            results[column] = {
                "status": "HAS DUPLICATES",
                "duplicated_values": duplicated_values
            }

            print(
                f"\n{column}: "
                f"{len(duplicates)} duplicated values"
            )

            for value, count in duplicates.items():

                print(
                    f"  {format_value(value)} "
                    f"-> {count} times"
                )

    return results


# ============================================================
# RELATIONSHIPS
# ============================================================

def analyze_group_relationships(df, columns):

    group_results = {}

    available_columns = [
        column
        for column in columns
        if column in df.columns
    ]

    if len(available_columns) < 2:
        return group_results

    for i, column_a in enumerate(
        available_columns
    ):

        for column_b in available_columns[
            i + 1:
        ]:

            temp = df[
                [column_a, column_b]
            ].copy()

            temp[column_a] = temp[
                column_a
            ].apply(normalize_value)

            temp[column_b] = temp[
                column_b
            ].apply(normalize_value)

            pair_key = (
                f"{column_a} -> {column_b}"
            )

            reverse_pair_key = (
                f"{column_b} -> {column_a}"
            )

            group_results[pair_key] = {
                "one_to_many_count": 0,
                "examples": []
            }

            group_results[reverse_pair_key] = {
                "one_to_many_count": 0,
                "examples": []
            }

            print(
                f"\n{column_a} <-> {column_b}"
            )

            # =================================================
            # A -> B
            # =================================================

            mapping_a_to_b = (
                temp.groupby(
                    column_a,
                    dropna=False
                )[column_b]
                .nunique()
            )

            multiple_a_to_b = (
                mapping_a_to_b[
                    mapping_a_to_b > 1
                ]
            )

            group_results[pair_key][
                "one_to_many_count"
            ] = int(
                len(multiple_a_to_b)
            )

            if multiple_a_to_b.empty:

                print(
                    f"  {column_a} -> {column_b}: "
                    "No one-to-many relationship"
                )

            else:

                print(
                    f"  {column_a} -> {column_b}: "
                    f"{len(multiple_a_to_b)} values "
                    f"have multiple {column_b} values"
                )

                for value_a in (
                    multiple_a_to_b.index[:10]
                ):

                    related_values = (
                        temp.loc[
                            temp[column_a] == value_a,
                            column_b
                        ]
                        .drop_duplicates()
                        .tolist()
                    )

                    formatted_related = [
                        format_value(value)
                        for value in related_values
                    ]

                    example = {
                        column_a: format_value(value_a),
                        column_b: formatted_related
                    }

                    group_results[pair_key][
                        "examples"
                    ].append(example)

                    print(
                        f"    {column_a} = "
                        f"{format_value(value_a)}"
                    )

                    print(
                        f"      {column_b}: "
                        f"{formatted_related}"
                    )

            # =================================================
            # B -> A
            # =================================================

            mapping_b_to_a = (
                temp.groupby(
                    column_b,
                    dropna=False
                )[column_a]
                .nunique()
            )

            multiple_b_to_a = (
                mapping_b_to_a[
                    mapping_b_to_a > 1
                ]
            )

            group_results[reverse_pair_key][
                "one_to_many_count"
            ] = int(
                len(multiple_b_to_a)
            )

            if multiple_b_to_a.empty:

                print(
                    f"  {column_b} -> {column_a}: "
                    "No one-to-many relationship"
                )

            else:

                print(
                    f"  {column_b} -> {column_a}: "
                    f"{len(multiple_b_to_a)} values "
                    f"have multiple {column_a} values"
                )

                for value_b in (
                    multiple_b_to_a.index[:10]
                ):

                    related_values = (
                        temp.loc[
                            temp[column_b] == value_b,
                            column_a
                        ]
                        .drop_duplicates()
                        .tolist()
                    )

                    formatted_related = [
                        format_value(value)
                        for value in related_values
                    ]

                    example = {
                        column_b: format_value(value_b),
                        column_a: formatted_related
                    }

                    group_results[
                        reverse_pair_key
                    ][
                        "examples"
                    ].append(example)

                    print(
                        f"    {column_b} = "
                        f"{format_value(value_b)}"
                    )

                    print(
                        f"      {column_a}: "
                        f"{formatted_related}"
                    )

    return group_results


def analyze_duplication_relationships(df):

    results = {}

    print("\n" + "-" * 100)
    print("DUPLICATION RELATIONSHIP ANALYSIS")
    print("-" * 100)

    for group_number, columns in enumerate(
        RELATIONSHIP_GROUPS,
        start=1
    ):

        group_name = (
            f"GROUP {group_number}: "
            f"{' <-> '.join(columns)}"
        )

        print(
            f"\n{group_name}"
        )

        print("-" * 80)

        results[group_name] = (
            analyze_group_relationships(
                df,
                columns
            )
        )

    return results


# ============================================================
# COMPLETE DUPLICATE ANALYSIS
# ============================================================

def analyze_duplication(df):

    duplicate_results = analyze_duplicates(df)

    relationship_results = (
        analyze_duplication_relationships(df)
    )

    return {
        "duplicates": duplicate_results,
        "relationships": relationship_results
    }


# ============================================================
# COLUMN STATUS
# ============================================================

def get_column_status(
    files_present,
    files_missing,
    null_values,
    empty_values
):

    if files_present == 0:
        return "MISSING FROM ALL FILES"

    has_nulls = null_values > 0
    has_empty = empty_values > 0
    missing_some_files = files_missing > 0

    if missing_some_files:

        if has_nulls and has_empty:
            return (
                "MISSING FROM SOME FILES "
                "+ HAS NULLS + EMPTY VALUES"
            )

        if has_nulls:
            return (
                "MISSING FROM SOME FILES "
                "+ HAS NULLS"
            )

        if has_empty:
            return (
                "MISSING FROM SOME FILES "
                "+ HAS EMPTY VALUES"
            )

        return "MISSING FROM SOME FILES"

    if has_nulls and has_empty:
        return "HAS NULLS + EMPTY VALUES"

    if has_nulls:
        return "HAS NULLS"

    if has_empty:
        return "HAS EMPTY VALUES"

    return "NEVER NULL / NEVER EMPTY"


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 100)
    print("R2 RESTAURANT DATA ANALYSIS")
    print("=" * 100)

    print(
        f"\nBucket : {BUCKET_NAME}"
    )

    print(
        f"Prefix : {R2_PREFIX}"
    )

    # ========================================================
    # LIST FILES
    # ========================================================

    files = []

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    for page in paginator.paginate(
        Bucket=BUCKET_NAME,
        Prefix=R2_PREFIX
    ):

        for obj in page.get(
            "Contents",
            []
        ):

            key = obj["Key"]

            if key.lower().endswith(".json"):
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

    # ========================================================
    # GLOBAL CONTAINERS
    # ========================================================

    global_unique_values = defaultdict(set)

    global_rating_data = []

    processed_files = []

    file_results = []

    # ========================================================
    # COLUMN COMPLETENESS
    # ========================================================

    column_stats = {
        column: {
            "files_present": 0,
            "files_missing": 0,
            "total_records": 0,
            "actual_values": 0,
            "null_values": 0,
            "empty_values": 0,
        }
        for column in COLUMN_ORDER
    }

    # ========================================================
    # PROCESS FILES
    # ========================================================

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

            file_result = {
                "file_number": file_number,
                "file_path": file_key,
                "status": "SUCCESS",
                "record_count": len(records),
                "min_max": {},
                "duplicates": {},
                "relationships": {}
            }

            if not records:

                print(
                    "\nNo records found."
                )

                file_result[
                    "status"
                ] = "NO RECORDS"

                processed_files.append(
                    file_key
                )

                file_results.append(
                    file_result
                )

                continue

            df = pd.DataFrame(records)

            print(
                f"\nRecords: {len(df)}"
            )

            print(
                f"Columns found: "
                f"{len(df.columns)}"
            )

            processed_files.append(
                file_key
            )

            # =================================================
            # COLUMN COMPLETENESS
            # =================================================

            for column in COLUMN_ORDER:

                stats = column_stats[column]

                stats[
                    "total_records"
                ] += len(df)

                if column not in df.columns:

                    stats[
                        "files_missing"
                    ] += 1

                    continue

                stats[
                    "files_present"
                ] += 1

                for value in df[column]:

                    if is_null_value(value):

                        stats[
                            "null_values"
                        ] += 1

                    elif is_empty_value(value):

                        stats[
                            "empty_values"
                        ] += 1

                    else:

                        stats[
                            "actual_values"
                        ] += 1

            # =================================================
            # GLOBAL UNIQUE VALUES
            # =================================================

            for column in UNIQUE_COLUMNS:

                if column not in df.columns:
                    continue

                values = get_unique_values(
                    df[column]
                )

                global_unique_values[
                    column
                ].update(values)

            # =================================================
            # MIN / MAX
            # =================================================

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

                    file_result[
                        "min_max"
                    ][column] = {
                        "status": "COLUMN NOT FOUND"
                    }

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

                    file_result[
                        "min_max"
                    ][column] = {
                        "status": "NO NUMERIC VALUES"
                    }

                    continue

                minimum = numeric_values.min()
                maximum = numeric_values.max()

                print(
                    f"\n{column}:"
                )

                print(
                    f"  Min: {minimum}"
                )

                print(
                    f"  Max: {maximum}"
                )

                file_result[
                    "min_max"
                ][column] = {
                    "min": json_safe_value(minimum),
                    "max": json_safe_value(maximum)
                }

            # =================================================
            # DUPLICATES + RELATIONSHIPS
            # =================================================

            duplication_result = (
                analyze_duplication(df)
            )

            file_result[
                "duplicates"
            ] = duplication_result[
                "duplicates"
            ]

            file_result[
                "relationships"
            ] = duplication_result[
                "relationships"
            ]

            # =================================================
            # RATING DATA
            # =================================================

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

            file_results.append(
                file_result
            )

        except Exception as e:

            print(
                "\nERROR processing file:"
            )

            print(
                f"  {e}"
            )

            file_results.append({
                "file_number": file_number,
                "file_path": file_key,
                "status": "ERROR",
                "error": str(e)
            })

    # ========================================================
    # GLOBAL UNIQUE VALUES
    # ========================================================

    print("\n\n")
    print("#" * 100)
    print("GLOBAL UNIQUE VALUES — ALL FILES")
    print("#" * 100)

    global_unique_output = {}

    for column in UNIQUE_COLUMNS:

        values = sorted(
            global_unique_values[column]
        )

        global_unique_output[column] = values

        print("\n" + "-" * 100)

        print(
            f"{column} "
            f"({len(values)} unique values "
            f"across ALL files):"
        )

        if not values:

            print(
                "  [No actual values]"
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

    rating_result = {
        "basic_statistics": {},
        "comparison": {}
    }

    if global_rating_data:

        all_ratings = pd.concat(
            global_rating_data,
            ignore_index=True
        )

        print(
            "\nBasic statistics:"
        )

        for column in RATING_COLUMNS:

            values = all_ratings[
                column
            ].dropna()

            if values.empty:

                rating_result[
                    "basic_statistics"
                ][column] = {
                    "min": None,
                    "max": None,
                    "unique": 0
                }

                print(
                    f"\n{column}:"
                )

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

            minimum = values.min()
            maximum = values.max()
            unique = values.nunique()

            rating_result[
                "basic_statistics"
            ][column] = {
                "min": json_safe_value(minimum),
                "max": json_safe_value(maximum),
                "unique": int(unique)
            }

            print(
                f"\n{column}:"
            )

            print(
                f"  Min    : {minimum}"
            )

            print(
                f"  Max    : {maximum}"
            )

            print(
                f"  Unique : {unique}"
            )

        # ----------------------------------------------------
        # COMPARISON
        # ----------------------------------------------------

        comparison_df = all_ratings.dropna(
            subset=RATING_COLUMNS
        )

        equal_count = (
            comparison_df[
                "totalReviews"
            ]
            ==
            comparison_df[
                "totalRatings"
            ]
        ).sum()

        reviews_greater = (
            comparison_df[
                "totalReviews"
            ]
            >
            comparison_df[
                "totalRatings"
            ]
        ).sum()

        ratings_greater = (
            comparison_df[
                "totalRatings"
            ]
            >
            comparison_df[
                "totalReviews"
            ]
        ).sum()

        rating_result[
            "comparison"
        ] = {
            "totalReviews == totalRatings":
                int(equal_count),

            "totalReviews > totalRatings":
                int(reviews_greater),

            "totalRatings > totalReviews":
                int(ratings_greater),
        }

        print(
            "\nComparison:"
        )

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

        rating_result[
            "status"
        ] = "NO VALID RATING DATA"

    # ========================================================
    # COLUMN COMPLETENESS
    # ========================================================

    print("\n\n")
    print("#" * 100)
    print("COLUMN COMPLETENESS — ALL FILES")
    print("#" * 100)

    total_files = len(files)

    column_completeness = {}

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

        actual_values = stats[
            "actual_values"
        ]

        null_values = stats[
            "null_values"
        ]

        empty_values = stats[
            "empty_values"
        ]

        status = get_column_status(
            files_present=files_present,
            files_missing=files_missing,
            null_values=null_values,
            empty_values=empty_values,
        )

        if total_records > 0:

            actual_percentage = (
                actual_values
                / total_records
                * 100
            )

            null_percentage = (
                null_values
                / total_records
                * 100
            )

            empty_percentage = (
                empty_values
                / total_records
                * 100
            )

        else:

            actual_percentage = 0
            null_percentage = 0
            empty_percentage = 0

        column_completeness[column] = {
            "status": status,
            "files_present": files_present,
            "files_missing": files_missing,
            "total_records": total_records,
            "actual_values": actual_values,
            "null_values": null_values,
            "empty_values": empty_values,
            "actual_percentage": round(
                actual_percentage,
                2
            ),
            "null_percentage": round(
                null_percentage,
                2
            ),
            "empty_percentage": round(
                empty_percentage,
                2
            )
        }

        print(
            "\n" + "-" * 100
        )

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
            f"  Actual Values   : "
            f"{actual_values}"
        )

        print(
            f"  NULL            : "
            f"{null_values}"
        )

        print(
            f"  EMPTY           : "
            f"{empty_values}"
        )

        print(
            f"  Actual %        : "
            f"{actual_percentage:.2f}%"
        )

        print(
            f"  NULL %          : "
            f"{null_percentage:.2f}%"
        )

        print(
            f"  EMPTY %         : "
            f"{empty_percentage:.2f}%"
        )

    # ========================================================
    # BUILD FINAL JSON
    # ========================================================

    final_result = {
        "analysis_info": {
            "bucket": BUCKET_NAME,
            "prefix": R2_PREFIX,
            "total_files_found": len(files),
            "successfully_processed": len(
                processed_files
            ),
            "output_file": OUTPUT_FILE
        },

        "files": file_results,

        "global_unique_values": (
            global_unique_output
        ),

        "global_rating_analysis": (
            rating_result
        ),

        "column_completeness": (
            column_completeness
        )
    }

    # ========================================================
    # SAVE JSON
    # ========================================================

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            final_result,
            f,
            ensure_ascii=False,
            indent=2
        )

    print("\n\n")
    print("=" * 100)
    print("ANALYSIS COMPLETED")
    print("=" * 100)

    print(
        f"\nTotal files found      : "
        f"{len(files)}"
    )

    print(
        f"Successfully processed : "
        f"{len(processed_files)}"
    )

    print(
        f"JSON result saved to   : "
        f"{OUTPUT_FILE}"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()