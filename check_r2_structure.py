import os
import boto3
from botocore.exceptions import ClientError

# Read credentials from environment variables
access_key_id = os.environ["CF_R2_ACCESS_KEY_ID"]
secret_access_key = os.environ["CF_R2_SECRET_ACCESS_KEY"]
endpoint_url = os.environ["CF_R2_ENDPOINT_URL"]
bucket_name = os.environ["CF_R2_BUCKET_NAME"]

# Folder to inspect
prefix = "merged-restaurant-info/"

# Create S3 client for Cloudflare R2
s3 = boto3.client(
    "s3",
    endpoint_url=endpoint_url,
    aws_access_key_id=access_key_id,
    aws_secret_access_key=secret_access_key,
    region_name="auto",
)

print("=" * 80)
print(f"Bucket: {bucket_name}")
print(f"Folder: {prefix}")
print("=" * 80)

try:
    paginator = s3.get_paginator("list_objects_v2")

    folders = set()
    files = []

    for page in paginator.paginate(
        Bucket=bucket_name,
        Prefix=prefix,
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]

            # Remove the root prefix
            relative_path = key[len(prefix):]

            if not relative_path:
                continue

            # Detect folders from the path
            parts = relative_path.split("/")

            if len(parts) > 1:
                current_path = prefix

                for part in parts[:-1]:
                    current_path += part + "/"
                    folders.add(current_path)

            # Ignore folder marker objects
            if not key.endswith("/"):
                files.append(
                    {
                        "key": key,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"],
                    }
                )

    print("\nSTRUCTURE")
    print("-" * 80)

    # Print folders
    for folder in sorted(folders):
        print(f"[DIR]  {folder}")

    # Print files
    for file in sorted(files, key=lambda x: x["key"]):
        size_mb = file["size"] / (1024 * 1024)

        print(
            f"[FILE] {file['key']} "
            f"| {size_mb:.2f} MB "
            f"| {file['last_modified']}"
        )

    print("\n" + "=" * 80)
    print(f"Folders found: {len(folders)}")
    print(f"Files found:   {len(files)}")
    print("=" * 80)

except ClientError as e:
    print("ERROR: Could not access Cloudflare R2")
    print(e)
    raise