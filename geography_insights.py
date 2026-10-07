import os
import re
import json
import unicodedata
from collections import defaultdict

import boto3


# =========================================================
# Configuration
# =========================================================

R2_PREFIX = "merged-restaurant-info/year=2025/month=09/day=17/"

SUMMARY_FILE = "geography_insights.json"
CUISINE_CONCENTRATION_FILE = "area_cuisine_concentration.json"
CUISINE_GAPS_FILE = "area_cuisine_gaps.json"
BRANCH_RESTAURANT_RATIO_FILE = "area_branch_restaurant_ratio.json"


# =========================================================
# R2 Client
# =========================================================

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["CF_R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
)


# =========================================================
# Helpers
# =========================================================

def clean(value):
    if value is None:
        return None

    if isinstance(value, str):
        value = unicodedata.normalize("NFKC", value).strip()

        if not value:
            return None

        return value

    return value


def sort_key(value):
    value = str(value)

    try:
        return (0, int(value))
    except ValueError:
        return (1, value.lower())


def first_segment(name):
    """
    Same concept used in the old market_insights logic.

    Example:
        "Pizza Hut, Kuwait" -> "Pizza Hut"
    """

    if not name:
        return None

    return name.split(",")[0].strip()


def brand_key(name):
    """
    Same normalization concept used by the old grouping logic.
    """

    if not name:
        return None

    name = unicodedata.normalize(
        "NFKC",
        name
    )

    name = name.replace(
        "'",
        ""
    )

    name = name.lower()

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    return name


def safe_div(numerator, denominator):
    if denominator == 0:
        return 0

    return numerator / denominator


def save_json(filename, data):

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def load_records(bucket, key):

    response = s3.get_object(
        Bucket=bucket,
        Key=key
    )

    content = response[
        "Body"
    ].read().decode("utf-8")

    data = json.loads(
        content
    )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return [data]

    return []


# =========================================================
# Restaurant Grouping
#
# SAME CONCEPT AS OLD market_insights.py
#
# 1. Get the first segment of restaurant names.
# 2. Normalize the brand key.
# 3. Keep brand -> restaurant IDs.
# 4. If the same restaurant ID contains multiple brand
#    keys, connect those brand keys.
# 5. Use union-find to create the final groups.
# =========================================================

def build_restaurant_groups(
    id_to_names
):

    # -----------------------------------------------------
    # restaurant ID -> brand keys
    # -----------------------------------------------------

    id_to_brands = defaultdict(set)

    # -----------------------------------------------------
    # brand key -> restaurant IDs
    # -----------------------------------------------------

    brand_to_ids = defaultdict(set)

    for restaurant_id, names in id_to_names.items():

        for name in names:

            segment = first_segment(
                name
            )

            normalized_brand = brand_key(
                segment
            )

            if not normalized_brand:
                continue

            id_to_brands[
                restaurant_id
            ].add(
                normalized_brand
            )

            brand_to_ids[
                normalized_brand
            ].add(
                restaurant_id
            )

    # -----------------------------------------------------
    # Union-find
    # -----------------------------------------------------

    parent = {}

    for brand in brand_to_ids:

        parent[brand] = brand

    def find(x):

        if parent[x] != x:

            parent[x] = find(
                parent[x]
            )

        return parent[x]

    def union(a, b):

        root_a = find(a)
        root_b = find(b)

        if root_a != root_b:

            parent[root_b] = root_a

    # -----------------------------------------------------
    # Same restaurant ID may contain multiple brand keys.
    # Connect those brand keys.
    # -----------------------------------------------------

    for restaurant_id, brands in (
        id_to_brands.items()
    ):

        brands = list(brands)

        if len(brands) < 2:
            continue

        first_brand = brands[0]

        for brand in brands[1:]:

            union(
                first_brand,
                brand
            )

    # -----------------------------------------------------
    # Build brand groups
    # -----------------------------------------------------

    brand_groups = defaultdict(set)

    for brand in brand_to_ids:

        root = find(
            brand
        )

        brand_groups[
            root
        ].add(
            brand
        )

    # -----------------------------------------------------
    # Convert brand groups into restaurant groups
    # -----------------------------------------------------

    grouped = {}

    for brand_group in (
        brand_groups.values()
    ):

        group_ids = set()

        group_names = set()

        for brand in brand_group:

            group_ids.update(
                brand_to_ids[
                    brand
                ]
            )

            for restaurant_id in (
                brand_to_ids[
                    brand
                ]
            ):

                group_names.update(
                    id_to_names.get(
                        restaurant_id,
                        set()
                    )
                )

        if not group_ids:
            continue

        # Keep the shortest available actual name
        # as display name, same concept as old script.
        display_name = sorted(
            group_names,
            key=lambda x: (
                len(x),
                x.lower()
            )
        )[0] if group_names else sorted(
            brand_group,
            key=lambda x: (
                len(x),
                x.lower()
            )
        )[0]

        grouped[
            display_name
        ] = {
            "ids": sorted(
                group_ids,
                key=sort_key
            ),

            "names": sorted(
                group_names,
                key=lambda x: x.lower()
            ),

            "brands": sorted(
                brand_group
            )
        }

    return grouped


# =========================================================
# Main
# =========================================================

def main():

    bucket = os.environ[
        "CF_R2_BUCKET_NAME"
    ]

    print("=" * 70)
    print("GEOGRAPHY INSIGHTS")
    print("=" * 70)

    # =====================================================
    # 1. Get JSON files from R2
    # =====================================================

    json_keys = []

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=R2_PREFIX
    ):

        for obj in page.get(
            "Contents",
            []
        ):

            key = obj[
                "Key"
            ]

            if key.lower().endswith(
                ".json"
            ):

                json_keys.append(
                    key
                )

    json_keys.sort()

    print(
        f"\nJSON files found: "
        f"{len(json_keys)}"
    )

    if not json_keys:

        print(
            "No JSON files found."
        )

        return

    # =====================================================
    # 2. Containers
    # =====================================================

    records = []

    # Restaurant ID -> original names
    id_to_names = defaultdict(set)

    # Area -> restaurant IDs
    area_to_restaurant_ids = defaultdict(set)

    # Area -> branch IDs
    area_to_branch_ids = defaultdict(set)

    # Area -> city
    area_to_cities = defaultdict(set)

    # Restaurant ID -> areas
    restaurant_id_to_areas = defaultdict(set)

    # Restaurant ID -> cuisines
    restaurant_id_to_cuisines = defaultdict(set)

    # =====================================================
    # 3. Read all files
    # =====================================================

    for index, key in enumerate(
        json_keys,
        start=1
    ):

        print(
            f"\rProcessing "
            f"{index}/{len(json_keys)}",
            end=""
        )

        file_records = load_records(
            bucket,
            key
        )

        records.extend(
            file_records
        )

        for r in file_records:

            restaurant_id = clean(
                r.get("id")
            )

            restaurant_name = clean(
                r.get("name")
            )

            branch_id = clean(
                r.get("branchId")
            )

            shop_area = clean(
                r.get("shopArea")
            )

            shop_city = clean(
                r.get("shopCity")
            )

            # -------------------------------------------------
            # Restaurant ID / Name
            # -------------------------------------------------

            if (
                restaurant_id
                and restaurant_name
            ):

                id_to_names[
                    restaurant_id
                ].add(
                    restaurant_name
                )

            # -------------------------------------------------
            # Area / Restaurant
            # -------------------------------------------------

            if (
                shop_area
                and restaurant_id
            ):

                area_to_restaurant_ids[
                    shop_area
                ].add(
                    restaurant_id
                )

                restaurant_id_to_areas[
                    restaurant_id
                ].add(
                    shop_area
                )

            # -------------------------------------------------
            # Area / Branch
            # -------------------------------------------------

            if (
                shop_area
                and branch_id
            ):

                area_to_branch_ids[
                    shop_area
                ].add(
                    branch_id
                )

            # -------------------------------------------------
            # Area / City
            # -------------------------------------------------

            if (
                shop_area
                and shop_city
            ):

                area_to_cities[
                    shop_area
                ].add(
                    shop_city
                )

            # -------------------------------------------------
            # Cuisine
            #
            # IMPORTANT:
            # Use cuisineString, not cuisines.
            #
            # Example:
            # "Healthy, Sandwiches, Burgers"
            #
            # -> Healthy
            # -> Sandwiches
            # -> Burgers
            # -------------------------------------------------

            cuisine_string = clean(
                r.get(
                    "cuisineString"
                )
            )

            cleaned_cuisines = set()

            if cuisine_string:

                for cuisine in (
                    cuisine_string.split(",")
                ):

                    cuisine = clean(
                        cuisine
                    )

                    if cuisine:

                        cleaned_cuisines.add(
                            cuisine
                        )

            if restaurant_id:

                restaurant_id_to_cuisines[
                    restaurant_id
                ].update(
                    cleaned_cuisines
                )

    print()

    print(
        f"Total records: "
        f"{len(records):,}"
    )

    # =====================================================
    # 4. SAME OLD RESTAURANT GROUPING
    # =====================================================

    grouped = build_restaurant_groups(
        id_to_names
    )

    unique_restaurants_after_grouping = len(
        grouped
    )

    print(
        f"unique_restaurants_after_grouping"
        f"     : "
        f"{unique_restaurants_after_grouping:,}"
    )

    # =====================================================
    # 5. Map grouped restaurants to areas
    # =====================================================

    area_to_grouped_restaurants = defaultdict(set)

    grouped_restaurant_to_areas = defaultdict(set)

    for display_name, group in (
        grouped.items()
    ):

        group_areas = set()

        for restaurant_id in (
            group["ids"]
        ):

            group_areas.update(
                restaurant_id_to_areas.get(
                    restaurant_id,
                    set()
                )
            )

        for area in group_areas:

            area_to_grouped_restaurants[
                area
            ].add(
                display_name
            )

            grouped_restaurant_to_areas[
                display_name
            ].add(
                area
            )

    # =====================================================
    # 6. Grouped restaurant -> cuisines
    # =====================================================

    grouped_restaurant_to_cuisines = defaultdict(set)

    for display_name, group in (
        grouped.items()
    ):

        for restaurant_id in (
            group["ids"]
        ):

            grouped_restaurant_to_cuisines[
                display_name
            ].update(
                restaurant_id_to_cuisines.get(
                    restaurant_id,
                    set()
                )
            )

    # =====================================================
    # 7. Area -> Cuisine -> Actual restaurants
    # =====================================================

    area_cuisine_restaurants = defaultdict(
        lambda: defaultdict(set)
    )

    for display_name in grouped:

        cuisines = (
            grouped_restaurant_to_cuisines[
                display_name
            ]
        )

        areas = (
            grouped_restaurant_to_areas[
                display_name
            ]
        )

        for area in areas:

            for cuisine in cuisines:

                area_cuisine_restaurants[
                    area
                ][cuisine].add(
                    display_name
                )

    # =====================================================
    # ANALYSIS 1
    #
    # Areas that concentrate certain cuisines
    # =====================================================

    print(
        "\nBuilding cuisine concentration..."
    )

    # Cuisine -> all actual/grouped restaurants
    cuisine_total_restaurants = defaultdict(set)

    for area, cuisine_data in (
        area_cuisine_restaurants.items()
    ):

        for cuisine, restaurant_names in (
            cuisine_data.items()
        ):

            cuisine_total_restaurants[
                cuisine
            ].update(
                restaurant_names
            )

    area_cuisine_concentration = {}

    for area in sorted(
        area_to_grouped_restaurants,
        key=sort_key
    ):

        cities = area_to_cities.get(
            area,
            set()
        )

        city = (
            sorted(
                cities,
                key=sort_key
            )[0]
            if cities
            else None
        )

        restaurant_count = len(
            area_to_grouped_restaurants[
                area
            ]
        )

        cuisine_results = []

        for cuisine in sorted(
            area_cuisine_restaurants[
                area
            ],
            key=lambda x: x.lower()
        ):

            cuisine_restaurants = (
                area_cuisine_restaurants[
                    area
                ][cuisine]
            )

            cuisine_count = len(
                cuisine_restaurants
            )

            area_percentage = safe_div(
                cuisine_count * 100,
                restaurant_count
            )

            total_with_cuisine = len(
                cuisine_total_restaurants[
                    cuisine
                ]
            )

            cuisine_global_percentage = safe_div(
                cuisine_count * 100,
                total_with_cuisine
            )

            cuisine_results.append({

                "cuisine":
                    cuisine,

                "restaurant_count":
                    cuisine_count,

                "percentage_of_area_restaurants":
                    round(
                        area_percentage,
                        2
                    ),

                "percentage_of_all_restaurants_with_cuisine":
                    round(
                        cuisine_global_percentage,
                        2
                    )
            })

        cuisine_results.sort(
            key=lambda x: (
                -x[
                    "percentage_of_area_restaurants"
                ],
                -x[
                    "restaurant_count"
                ],
                x[
                    "cuisine"
                ].lower()
            )
        )

        area_cuisine_concentration[
            area
        ] = {

            "shopCity":
                city,

            "shopArea":
                area,

            "restaurant_count":
                restaurant_count,

            "cuisines":
                cuisine_results
        }

    # -----------------------------------------------------
    # Top areas for each cuisine
    # -----------------------------------------------------

    top_areas_by_cuisine = defaultdict(list)

    for area, data in (
        area_cuisine_concentration.items()
    ):

        for cuisine_data in (
            data["cuisines"]
        ):

            cuisine = (
                cuisine_data[
                    "cuisine"
                ]
            )

            top_areas_by_cuisine[
                cuisine
            ].append({

                "shopCity":
                    data[
                        "shopCity"
                    ],

                "shopArea":
                    area,

                "restaurant_count":
                    cuisine_data[
                        "restaurant_count"
                    ],

                "percentage_of_area_restaurants":
                    cuisine_data[
                        "percentage_of_area_restaurants"
                    ]
            })

    for cuisine in top_areas_by_cuisine:

        top_areas_by_cuisine[
            cuisine
        ].sort(
            key=lambda x: (
                -x[
                    "percentage_of_area_restaurants"
                ],

                -x[
                    "restaurant_count"
                ],

                sort_key(
                    x[
                        "shopArea"
                    ]
                )
            )
        )

        top_areas_by_cuisine[
            cuisine
        ] = (
            top_areas_by_cuisine[
                cuisine
            ][:10]
        )

    # =====================================================
    # ANALYSIS 2
    #
    # Areas that lack / underrepresent cuisines
    # =====================================================

    print(
        "Building cuisine gaps..."
    )

    all_cuisines = sorted(
        cuisine_total_restaurants,
        key=lambda x: x.lower()
    )

    # Cuisine -> percentage in each area
    # where that cuisine exists
    cuisine_area_percentages = defaultdict(list)

    for area, data in (
        area_cuisine_concentration.items()
    ):

        for cuisine_data in (
            data["cuisines"]
        ):

            cuisine_area_percentages[
                cuisine_data[
                    "cuisine"
                ]
            ].append(
                cuisine_data[
                    "percentage_of_area_restaurants"
                ]
            )

    # Average share of cuisine across areas
    # where the cuisine exists
    cuisine_average_percentage = {}

    for cuisine, percentages in (
        cuisine_area_percentages.items()
    ):

        cuisine_average_percentage[
            cuisine
        ] = safe_div(
            sum(percentages),
            len(percentages)
        )

    area_cuisine_gaps = {}

    for area in sorted(
        area_to_grouped_restaurants,
        key=sort_key
    ):

        restaurant_count = len(
            area_to_grouped_restaurants[
                area
            ]
        )

        cities = area_to_cities.get(
            area,
            set()
        )

        city = (
            sorted(
                cities,
                key=sort_key
            )[0]
            if cities
            else None
        )

        gaps = []

        for cuisine in all_cuisines:

            count = len(
                area_cuisine_restaurants[
                    area
                ].get(
                    cuisine,
                    set()
                )
            )

            area_percentage = safe_div(
                count * 100,
                restaurant_count
            )

            average_percentage = (
                cuisine_average_percentage.get(
                    cuisine,
                    0
                )
            )

            gap = (
                average_percentage
                - area_percentage
            )

            if count == 0:

                gap_type = "missing"

            elif area_percentage < average_percentage:

                gap_type = "low"

            else:

                continue

            gaps.append({

                "cuisine":
                    cuisine,

                "restaurant_count":
                    count,

                "percentage_of_area_restaurants":
                    round(
                        area_percentage,
                        2
                    ),

                "average_percentage_in_areas_with_cuisine":
                    round(
                        average_percentage,
                        2
                    ),

                "gap_percentage_points":
                    round(
                        gap,
                        2
                    ),

                "gap_type":
                    gap_type
            })

        gaps.sort(
            key=lambda x: (
                -x[
                    "gap_percentage_points"
                ],

                x[
                    "restaurant_count"
                ],

                x[
                    "cuisine"
                ].lower()
            )
        )

        area_cuisine_gaps[
            area
        ] = {

            "shopCity":
                city,

            "shopArea":
                area,

            "restaurant_count":
                restaurant_count,

            "cuisine_gaps":
                gaps
        }

    # =====================================================
    # ANALYSIS 3
    #
    # Many branches vs few actual restaurants
    # =====================================================

    print(
        "Building branch / restaurant analysis..."
    )

    area_branch_restaurant_ratio = []

    for area in sorted(
        area_to_grouped_restaurants,
        key=sort_key
    ):

        branch_count = len(
            area_to_branch_ids.get(
                area,
                set()
            )
        )

        restaurant_count = len(
            area_to_grouped_restaurants[
                area
            ]
        )

        ratio = safe_div(
            branch_count,
            restaurant_count
        )

        cities = area_to_cities.get(
            area,
            set()
        )

        city = (
            sorted(
                cities,
                key=sort_key
            )[0]
            if cities
            else None
        )

        percentage_of_all_restaurants = safe_div(
            restaurant_count * 100,
            unique_restaurants_after_grouping
        )

        area_branch_restaurant_ratio.append({

            "shopCity":
                city,

            "shopArea":
                area,

            "branch_count":
                branch_count,

            "restaurant_count":
                restaurant_count,

            "branch_to_restaurant_ratio":
                round(
                    ratio,
                    2
                ),

            "percentage_of_all_grouped_restaurants":
                round(
                    percentage_of_all_restaurants,
                    2
                )
        })

    # Highest ratios
    highest_branch_restaurant_ratio = sorted(
        area_branch_restaurant_ratio,
        key=lambda x: (
            -x[
                "branch_to_restaurant_ratio"
            ],

            -x[
                "branch_count"
            ],

            x[
                "restaurant_count"
            ],

            sort_key(
                x[
                    "shopArea"
                ]
            )
        )
    )[:20]

    # =====================================================
    # 8. Save outputs
    # =====================================================

    cuisine_concentration_output = {

        "areas":
            area_cuisine_concentration,

        "top_areas_by_cuisine":
            dict(
                sorted(
                    top_areas_by_cuisine.items(),
                    key=lambda x: x[0].lower()
                )
            )
    }

    cuisine_gaps_output = {

        "areas":
            area_cuisine_gaps,

        "cuisines_analyzed":
            all_cuisines,

        "average_percentage_by_cuisine":
            {
                cuisine: round(
                    percentage,
                    2
                )

                for cuisine, percentage
                in sorted(
                    cuisine_average_percentage.items(),
                    key=lambda x: x[0].lower()
                )
            }
    }

    branch_restaurant_output = {

        "all_areas":
            area_branch_restaurant_ratio,

        "highest_branch_to_restaurant_ratio":
            highest_branch_restaurant_ratio
    }

    summary = {

        "dataset": {

            "json_files":
                len(json_keys),

            "records":
                len(records),

            "unique_restaurants_after_grouping":
                unique_restaurants_after_grouping,

            "unique_areas":
                len(
                    area_to_grouped_restaurants
                ),

            "unique_cuisines":
                len(
                    all_cuisines
                )
        },

        "analyses": {

            "cuisine_concentration":
                CUISINE_CONCENTRATION_FILE,

            "cuisine_gaps":
                CUISINE_GAPS_FILE,

            "branch_restaurant_ratio":
                BRANCH_RESTAURANT_RATIO_FILE
        }
    }

    save_json(
        CUISINE_CONCENTRATION_FILE,
        cuisine_concentration_output
    )

    save_json(
        CUISINE_GAPS_FILE,
        cuisine_gaps_output
    )

    save_json(
        BRANCH_RESTAURANT_RATIO_FILE,
        branch_restaurant_output
    )

    save_json(
        SUMMARY_FILE,
        summary
    )

    # =====================================================
    # 9. Console summary
    # =====================================================

    print()
    print("=" * 70)
    print("RESULT SUMMARY")
    print("=" * 70)

    print(
        f"\nJSON files"
        f"                         : "
        f"{len(json_keys):,}"
    )

    print(
        f"records"
        f"                            : "
        f"{len(records):,}"
    )

    print(
        f"unique_restaurants_after_grouping"
        f"     : "
        f"{unique_restaurants_after_grouping:,}"
    )

    print(
        f"unique_areas"
        f"                         : "
        f"{len(area_to_grouped_restaurants):,}"
    )

    print(
        f"unique_cuisines"
        f"                      : "
        f"{len(all_cuisines):,}"
    )

    # =====================================================
    # Top cuisine concentrations
    # =====================================================

    print(
        "\nTop cuisine concentrations:"
    )

    concentration_samples = []

    for area, data in (
        area_cuisine_concentration.items()
    ):

        for cuisine_data in (
            data["cuisines"][:3]
        ):

            concentration_samples.append({

                "shopCity":
                    data["shopCity"],

                "shopArea":
                    area,

                **cuisine_data
            })

    concentration_samples.sort(
        key=lambda x: (
            -x[
                "percentage_of_area_restaurants"
            ],

            -x[
                "restaurant_count"
            ]
        )
    )

    for item in concentration_samples[:20]:

        print(
            f"  City "
            f"{item['shopCity']}, "
            f"Area "
            f"{item['shopArea']} - "
            f"{item['cuisine']}: "
            f"{item['restaurant_count']:,} "
            f"restaurants "
            f"("
            f"{item['percentage_of_area_restaurants']:.2f}%"
            f")"
        )

    # =====================================================
    # Top cuisine gaps
    # =====================================================

    print(
        "\nStrongest cuisine gaps:"
    )

    gap_samples = []

    for area, data in (
        area_cuisine_gaps.items()
    ):

        for gap in data[
            "cuisine_gaps"
        ]:

            gap_samples.append({

                "shopCity":
                    data["shopCity"],

                "shopArea":
                    area,

                **gap
            })

    gap_samples.sort(
        key=lambda x: (
            -x[
                "gap_percentage_points"
            ],

            x[
                "restaurant_count"
            ]
        )
    )

    for item in gap_samples[:20]:

        print(
            f"  City "
            f"{item['shopCity']}, "
            f"Area "
            f"{item['shopArea']} - "
            f"{item['cuisine']}: "
            f"{item['gap_type']}, "
            f"gap "
            f"{item['gap_percentage_points']:.2f} pp"
        )

    # =====================================================
    # Branch / restaurant
    # =====================================================

    print(
        "\nHighest branch / restaurant ratios:"
    )

    for item in (
        highest_branch_restaurant_ratio
    ):

        print(
            f"  City "
            f"{item['shopCity']}, "
            f"Area "
            f"{item['shopArea']}: "
            f"{item['branch_count']:,} branches / "
            f"{item['restaurant_count']:,} restaurants "
            f"(ratio "
            f"{item['branch_to_restaurant_ratio']:.2f})"
        )

    # =====================================================
    # Saved files
    # =====================================================

    print()
    print("=" * 70)
    print("SAVED FILES")
    print("=" * 70)

    print(
        SUMMARY_FILE
    )

    print(
        CUISINE_CONCENTRATION_FILE
    )

    print(
        CUISINE_GAPS_FILE
    )

    print(
        BRANCH_RESTAURANT_RATIO_FILE
    )

    print(
        "\nDone."
    )


# =========================================================
# Run
# =========================================================

if __name__ == "__main__":
    main()