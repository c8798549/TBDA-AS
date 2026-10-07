import os
import re
import json
import unicodedata
from collections import defaultdict, Counter

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
    Same basic grouping idea used in the previous script:
    use the first meaningful segment of the restaurant name
    as the initial grouping key.
    """

    if not name:
        return None

    name = clean(name)

    if not name:
        return None

    parts = re.split(r"\s*[-|/,:]\s*", name)

    return parts[0].strip().lower()


def brand_key(name):
    """
    Normalize restaurant name for grouping.
    """

    if not name:
        return None

    name = clean(name).lower()

    name = re.sub(r"[^\w\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name


def safe_div(numerator, denominator):
    if denominator == 0:
        return 0

    return numerator / denominator


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
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

    content = response["Body"].read().decode("utf-8")

    data = json.loads(content)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return [data]

    return []


# =========================================================
# Restaurant Grouping
# =========================================================

def build_restaurant_groups(
    records,
    id_to_names,
    name_to_ids
):
    """
    Build grouped restaurants using restaurant IDs and names.

    A grouped restaurant represents the actual restaurant/brand,
    even if it has multiple IDs or multiple name variants.
    """

    parent = {}

    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])

        return parent[x]

    def union(a, b):
        root_a = find(a)
        root_b = find(b)

        if root_a != root_b:
            parent[root_b] = root_a

    # -----------------------------------------------------
    # Create nodes for IDs
    # -----------------------------------------------------

    for restaurant_id in id_to_names:
        parent[restaurant_id] = restaurant_id

    # -----------------------------------------------------
    # Connect IDs that share the same restaurant name
    # -----------------------------------------------------

    for name, ids in name_to_ids.items():

        ids = list(ids)

        if len(ids) < 2:
            continue

        first_id = ids[0]

        for restaurant_id in ids[1:]:
            union(first_id, restaurant_id)

    # -----------------------------------------------------
    # Build connected groups
    # -----------------------------------------------------

    groups = defaultdict(set)

    for restaurant_id in parent:
        root = find(restaurant_id)

        groups[root].add(restaurant_id)

    # -----------------------------------------------------
    # Build display information
    # -----------------------------------------------------

    grouped = {}

    for group_ids in groups.values():

        all_names = set()

        for restaurant_id in group_ids:
            all_names.update(
                id_to_names.get(restaurant_id, set())
            )

        if not all_names:
            continue

        display_name = sorted(
            all_names,
            key=lambda x: (
                len(x),
                x.lower()
            )
        )[0]

        grouped[display_name] = {
            "ids": sorted(
                group_ids,
                key=sort_key
            ),
            "names": sorted(
                all_names,
                key=lambda x: x.lower()
            )
        }

    return grouped


# =========================================================
# Main
# =========================================================

def main():

    bucket = os.environ["CF_R2_BUCKET_NAME"]

    print("=" * 70)
    print("AREA RESTAURANT ANALYSIS")
    print("=" * 70)

    # =====================================================
    # 1. Get JSON files from R2
    # =====================================================

    json_keys = []

    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]

            if key.lower().endswith(".json"):
                json_keys.append(key)

    json_keys.sort()

    print(f"\nJSON files found: {len(json_keys)}")

    if not json_keys:
        print("No JSON files found.")
        return

    # =====================================================
    # 2. Containers
    # =====================================================

    records = []

    id_to_names = defaultdict(set)
    name_to_ids = defaultdict(set)

    # area -> restaurant IDs
    area_to_restaurant_ids = defaultdict(set)

    # area -> branch IDs
    area_to_branch_ids = defaultdict(set)

    # area -> city
    area_to_cities = defaultdict(set)

    # restaurant ID -> areas
    restaurant_id_to_areas = defaultdict(set)

    # restaurant ID -> cuisines
    restaurant_id_to_cuisines = defaultdict(set)

    # area -> cuisine -> restaurant IDs
    area_cuisine_restaurants = defaultdict(
        lambda: defaultdict(set)
    )

    # =====================================================
    # 3. Read all files
    # =====================================================

    for index, key in enumerate(json_keys, start=1):

        print(
            f"\rProcessing {index}/{len(json_keys)}",
            end=""
        )

        file_records = load_records(
            bucket,
            key
        )

        records.extend(file_records)

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

            if restaurant_id and restaurant_name:

                id_to_names[
                    restaurant_id
                ].add(restaurant_name)

                name_to_ids[
                    brand_key(restaurant_name)
                ].add(restaurant_id)

            # -------------------------------------------------
            # Area / restaurant
            # -------------------------------------------------

            if shop_area and restaurant_id:

                area_to_restaurant_ids[
                    shop_area
                ].add(restaurant_id)

                restaurant_id_to_areas[
                    restaurant_id
                ].add(shop_area)

            # -------------------------------------------------
            # Area / branch
            # -------------------------------------------------

            if shop_area and branch_id:

                area_to_branch_ids[
                    shop_area
                ].add(branch_id)

            # -------------------------------------------------
            # Area / city
            # -------------------------------------------------

            if shop_area and shop_city:

                area_to_cities[
                    shop_area
                ].add(shop_city)

            # -------------------------------------------------
            # Cuisine
            # -------------------------------------------------

            cuisines = r.get("cuisines")

            if isinstance(cuisines, list):

                cleaned_cuisines = set()

                for cuisine in cuisines:

                    cuisine = clean(cuisine)

                    if cuisine:
                        cleaned_cuisines.add(cuisine)

            else:
                cleaned_cuisines = set()

            if restaurant_id:

                restaurant_id_to_cuisines[
                    restaurant_id
                ].update(
                    cleaned_cuisines
                )

    print()

    print(f"Total records: {len(records):,}")

    # =====================================================
    # 4. Group restaurants
    # =====================================================

    grouped = build_restaurant_groups(
        records,
        id_to_names,
        name_to_ids
    )

    print(
        f"Grouped restaurants: "
        f"{len(grouped):,}"
    )

    # =====================================================
    # 5. Map grouped restaurants to areas
    # =====================================================

    area_to_grouped_restaurants = defaultdict(set)

    grouped_restaurant_to_areas = defaultdict(set)

    for display_name, group in grouped.items():

        group_ids = group["ids"]

        group_areas = set()

        for restaurant_id in group_ids:

            group_areas.update(
                restaurant_id_to_areas.get(
                    restaurant_id,
                    set()
                )
            )

        for area in group_areas:

            area_to_grouped_restaurants[
                area
            ].add(display_name)

            grouped_restaurant_to_areas[
                display_name
            ].add(area)

    # =====================================================
    # 6. Build cuisine information for grouped restaurants
    # =====================================================

    grouped_restaurant_to_cuisines = defaultdict(set)

    for display_name, group in grouped.items():

        for restaurant_id in group["ids"]:

            grouped_restaurant_to_cuisines[
                display_name
            ].update(
                restaurant_id_to_cuisines.get(
                    restaurant_id,
                    set()
                )
            )

    # =====================================================
    # 7. Area / Cuisine
    # =====================================================

    for display_name, group in grouped.items():

        cuisines = grouped_restaurant_to_cuisines[
            display_name
        ]

        areas = grouped_restaurant_to_areas[
            display_name
        ]

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

    print("\nBuilding cuisine concentration analysis...")

    # cuisine -> total restaurants across all areas
    cuisine_total_restaurants = defaultdict(set)

    for area, cuisine_data in area_cuisine_restaurants.items():

        for cuisine, restaurant_names in cuisine_data.items():

            cuisine_total_restaurants[
                cuisine
            ].update(
                restaurant_names
            )

    area_cuisine_concentration = {}

    for area in sorted(
        area_cuisine_restaurants,
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

        area_total_restaurants = len(
            area_to_grouped_restaurants.get(
                area,
                set()
            )
        )

        cuisines_result = []

        for cuisine in sorted(
            area_cuisine_restaurants[area],
            key=lambda x: x.lower()
        ):

            restaurant_count = len(
                area_cuisine_restaurants[
                    area
                ][cuisine]
            )

            percentage_of_area = safe_div(
                restaurant_count * 100,
                area_total_restaurants
            )

            total_cuisine_restaurants = len(
                cuisine_total_restaurants[
                    cuisine
                ]
            )

            percentage_of_all_cuisine_restaurants = safe_div(
                restaurant_count * 100,
                total_cuisine_restaurants
            )

            cuisines_result.append({
                "cuisine": cuisine,
                "restaurant_count": restaurant_count,
                "percentage_of_area_restaurants":
                    round(
                        percentage_of_area,
                        2
                    ),
                "percentage_of_all_restaurants_with_cuisine":
                    round(
                        percentage_of_all_cuisine_restaurants,
                        2
                    )
            })

        # Highest concentration first
        cuisines_result.sort(
            key=lambda x: (
                -x[
                    "percentage_of_area_restaurants"
                ],
                -x[
                    "restaurant_count"
                ],
                x["cuisine"].lower()
            )
        )

        area_cuisine_concentration[
            area
        ] = {
            "shopCity": city,
            "shopArea": area,
            "restaurant_count": area_total_restaurants,
            "cuisines": cuisines_result
        }

    # =====================================================
    # 8. Identify strongest cuisine concentrations
    # =====================================================

    cuisine_concentration_extremes = defaultdict(list)

    for area, data in area_cuisine_concentration.items():

        for cuisine_data in data["cuisines"]:

            cuisine = cuisine_data["cuisine"]

            cuisine_concentration_extremes[
                cuisine
            ].append({
                "shopCity": data["shopCity"],
                "shopArea": area,
                "restaurant_count":
                    cuisine_data[
                        "restaurant_count"
                    ],
                "percentage_of_area_restaurants":
                    cuisine_data[
                        "percentage_of_area_restaurants"
                    ]
            })

    for cuisine in cuisine_concentration_extremes:

        cuisine_concentration_extremes[
            cuisine
        ].sort(
            key=lambda x: (
                -x[
                    "percentage_of_area_restaurants"
                ],
                -x["restaurant_count"],
                sort_key(x["shopCity"]),
                sort_key(x["shopArea"])
            )
        )

        cuisine_concentration_extremes[
            cuisine
        ] = cuisine_concentration_extremes[
            cuisine
        ][:10]

    # =====================================================
    # ANALYSIS 2
    #
    # Areas that lack certain cuisines
    # =====================================================

    print("Building cuisine gaps analysis...")

    all_cuisines = sorted(
        cuisine_total_restaurants,
        key=lambda x: x.lower()
    )

    area_cuisine_gaps = {}

    # -----------------------------------------------------
    # Calculate average area share for every cuisine
    # -----------------------------------------------------

    cuisine_area_percentages = defaultdict(list)

    for area, data in area_cuisine_concentration.items():

        area_total = data["restaurant_count"]

        for cuisine in all_cuisines:

            count = len(
                area_cuisine_restaurants[
                    area
                ].get(
                    cuisine,
                    set()
                )
            )

            percentage = safe_div(
                count * 100,
                area_total
            )

            if count > 0:
                cuisine_area_percentages[
                    cuisine
                ].append(
                    percentage
                )

    cuisine_average_percentage = {}

    for cuisine, percentages in cuisine_area_percentages.items():

        cuisine_average_percentage[
            cuisine
        ] = safe_div(
            sum(percentages),
            len(percentages)
        )

    # -----------------------------------------------------
    # Find gaps
    # -----------------------------------------------------

    for area in sorted(
        area_cuisine_concentration,
        key=sort_key
    ):

        data = area_cuisine_concentration[
            area
        ]

        area_total = data[
            "restaurant_count"
        ]

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

            percentage = safe_div(
                count * 100,
                area_total
            )

            average_percentage = (
                cuisine_average_percentage.get(
                    cuisine,
                    0
                )
            )

            # -------------------------------------------------
            # Gap score
            #
            # How much lower is the area's cuisine share
            # compared with the average share of that cuisine
            # in areas where it exists.
            # -------------------------------------------------

            if average_percentage > 0:

                gap_percentage = (
                    average_percentage
                    - percentage
                )

            else:
                gap_percentage = 0

            if count == 0:

                gap_type = "missing"

            elif percentage < average_percentage:

                gap_type = "low"

            else:

                continue

            gaps.append({
                "cuisine": cuisine,
                "restaurant_count": count,
                "percentage_of_area_restaurants":
                    round(
                        percentage,
                        2
                    ),
                "average_percentage_in_areas_with_cuisine":
                    round(
                        average_percentage,
                        2
                    ),
                "gap_percentage_points":
                    round(
                        gap_percentage,
                        2
                    ),
                "gap_type": gap_type
            })

        gaps.sort(
            key=lambda x: (
                -x["gap_percentage_points"],
                x["restaurant_count"],
                x["cuisine"].lower()
            )
        )

        area_cuisine_gaps[
            area
        ] = {
            "shopCity": city,
            "shopArea": area,
            "restaurant_count": area_total,
            "cuisine_gaps": gaps
        }

    # =====================================================
    # ANALYSIS 3
    #
    # Areas with many branches but few actual restaurants
    # =====================================================

    print(
        "Building branch / restaurant ratio analysis..."
    )

    area_branch_restaurant_ratio = []

    total_grouped_restaurants = len(
        grouped
    )

    for area in sorted(
        area_to_branch_ids,
        key=sort_key
    ):

        branch_count = len(
            area_to_branch_ids[
                area
            ]
        )

        restaurant_count = len(
            area_to_grouped_restaurants.get(
                area,
                set()
            )
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

        restaurant_percentage = safe_div(
            restaurant_count * 100,
            total_grouped_restaurants
        )

        area_branch_restaurant_ratio.append({
            "shopCity": city,
            "shopArea": area,
            "branch_count": branch_count,
            "restaurant_count": restaurant_count,
            "branch_to_restaurant_ratio":
                round(
                    ratio,
                    2
                ),
            "percentage_of_all_grouped_restaurants":
                round(
                    restaurant_percentage,
                    2
                )
        })

    # -----------------------------------------------------
    # Highest ratios
    # -----------------------------------------------------

    highest_branch_restaurant_ratio = sorted(
        area_branch_restaurant_ratio,
        key=lambda x: (
            -x[
                "branch_to_restaurant_ratio"
            ],
            -x["branch_count"],
            x["restaurant_count"],
            sort_key(x["shopCity"]),
            sort_key(x["shopArea"])
        )
    )[:20]

    # -----------------------------------------------------
    # Areas with high branch counts + low restaurant counts
    #
    # We first rank by ratio, while retaining the actual
    # branch and restaurant counts.
    # -----------------------------------------------------

    high_branch_low_restaurant_areas = sorted(
        area_branch_restaurant_ratio,
        key=lambda x: (
            -x[
                "branch_to_restaurant_ratio"
            ],
            -x["branch_count"],
            x["restaurant_count"],
            sort_key(x["shopCity"]),
            sort_key(x["shopArea"])
        )
    )[:20]

    # =====================================================
    # 9. Save outputs
    # =====================================================

    cuisine_concentration_output = {
        "areas": area_cuisine_concentration,
        "top_areas_by_cuisine": dict(
            sorted(
                cuisine_concentration_extremes.items(),
                key=lambda x: x[0].lower()
            )
        )
    }

    cuisine_gaps_output = {
        "areas": area_cuisine_gaps,
        "cuisines_analyzed": all_cuisines
    }

    branch_restaurant_output = {
        "all_areas": area_branch_restaurant_ratio,
        "highest_branch_to_restaurant_ratio":
            highest_branch_restaurant_ratio,
        "high_branch_low_restaurant_areas":
            high_branch_low_restaurant_areas
    }

    summary = {

        "dataset": {
            "json_files": len(json_keys),
            "records": len(records),
            "grouped_restaurants":
                total_grouped_restaurants,
            "unique_areas":
                len(area_to_grouped_restaurants),
            "unique_cuisines":
                len(all_cuisines)
        },

        "areas_with_cuisine_concentration": {
            "description":
                "Cuisine distribution and concentration for each area.",
            "file":
                CUISINE_CONCENTRATION_FILE
        },

        "areas_with_cuisine_gaps": {
            "description":
                "Cuisines that are missing or underrepresented in each area.",
            "file":
                CUISINE_GAPS_FILE
        },

        "areas_with_many_branches_few_restaurants": {
            "description":
                "Areas ranked by branch-to-restaurant ratio.",
            "file":
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
    # 10. Console summary
    # =====================================================

    print("\n" + "=" * 70)
    print("RESULT SUMMARY")
    print("=" * 70)

    print(
        f"\nJSON files: "
        f"{len(json_keys):,}"
    )

    print(
        f"Records: "
        f"{len(records):,}"
    )

    print(
        f"Grouped restaurants: "
        f"{total_grouped_restaurants:,}"
    )

    print(
        f"Areas: "
        f"{len(area_to_grouped_restaurants):,}"
    )

    print(
        f"Cuisines: "
        f"{len(all_cuisines):,}"
    )

    # -----------------------------------------------------
    # Cuisine concentration samples
    # -----------------------------------------------------

    print(
        "\nTop cuisine concentrations by area:"
    )

    for area in sorted(
        area_cuisine_concentration,
        key=sort_key
    )[:10]:

        data = area_cuisine_concentration[
            area
        ]

        print(
            f"\n  City {data['shopCity']}, "
            f"Area {area}"
        )

        for cuisine in data["cuisines"][:5]:

            print(
                f"    {cuisine['cuisine']}: "
                f"{cuisine['restaurant_count']:,} "
                f"restaurants "
                f"({cuisine['percentage_of_area_restaurants']:.2f}%)"
            )

    # -----------------------------------------------------
    # Cuisine gaps samples
    # -----------------------------------------------------

    print(
        "\nAreas with strongest cuisine gaps:"
    )

    gap_samples = []

    for area, data in area_cuisine_gaps.items():

        for gap in data["cuisine_gaps"]:

            gap_samples.append({
                "shopCity":
                    data["shopCity"],
                "shopArea":
                    area,
                **gap
            })

    gap_samples.sort(
        key=lambda x: (
            -x["gap_percentage_points"],
            x["restaurant_count"],
            sort_key(x["shopCity"]),
            sort_key(x["shopArea"])
        )
    )

    for gap in gap_samples[:20]:

        print(
            f"  City {gap['shopCity']}, "
            f"Area {gap['shopArea']} - "
            f"{gap['cuisine']}: "
            f"{gap['restaurant_count']} restaurants "
            f"({gap['gap_type']}, "
            f"gap {gap['gap_percentage_points']:.2f} pp)"
        )

    # -----------------------------------------------------
    # Branch / restaurant ratio
    # -----------------------------------------------------

    print(
        "\nAreas with many branches but few actual restaurants:"
    )

    for area in high_branch_low_restaurant_areas:

        print(
            f"  City {area['shopCity']}, "
            f"Area {area['shopArea']}: "
            f"{area['branch_count']:,} branches / "
            f"{area['restaurant_count']:,} restaurants "
            f"(ratio "
            f"{area['branch_to_restaurant_ratio']:.2f})"
        )

    # =====================================================
    # 11. Saved files
    # =====================================================

    print("\n" + "=" * 70)
    print("SAVED FILES")
    print("=" * 70)

    print(
        f"{SUMMARY_FILE:<40} "
        f"overall summary"
    )

    print(
        f"{CUISINE_CONCENTRATION_FILE:<40} "
        f"cuisine concentration by area"
    )

    print(
        f"{CUISINE_GAPS_FILE:<40} "
        f"cuisine gaps by area"
    )

    print(
        f"{BRANCH_RESTAURANT_RATIO_FILE:<40} "
        f"branch / restaurant ratio by area"
    )

    print("\nDone.")


# =========================================================
# Run
# =========================================================

if __name__ == "__main__":
    main()