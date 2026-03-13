#!/usr/bin/env python3
"""Delete all nzshm-backup-managed S3 buckets in the current account.

Finds every bucket tagged ManagedBy=nzshm-backup, removes the no-delete
bucket policy (which blocks s3:DeleteObject even for admins), empties the
bucket, then deletes it.

Run this when migrating from the old naming scheme to the new bb-* scheme,
or when tearing down a sandbox.

Usage:
    # Dry-run — list buckets that would be deleted (no changes made)
    python scripts/cleanup-backup-buckets.py --dry-run

    # With explicit AWS profile (spike/backup account)
    python scripts/cleanup-backup-buckets.py --profile spike-admin --dry-run
    python scripts/cleanup-backup-buckets.py --profile spike-admin

    # Filter to only old-scheme buckets (nzshm-dynamo-backup-* and *-backup-ap-*)
    python scripts/cleanup-backup-buckets.py --profile spike-admin --old-names-only --dry-run
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

MANAGED_BY_TAG = "ManagedBy"
MANAGED_BY_VALUE = "nzshm-backup"

# Old naming patterns (pre bb-* redesign)
OLD_PREFIXES = ("nzshm-dynamo-backup-",)
OLD_SUFFIXES = ("-backup-ap-southeast-2-",)


def is_old_scheme(bucket_name: str) -> bool:
    if any(bucket_name.startswith(p) for p in OLD_PREFIXES):
        return True
    if any(p in bucket_name for p in OLD_SUFFIXES):
        return True
    return False


def get_managed_buckets(s3_client, old_names_only: bool) -> list[str]:
    """Return names of all buckets tagged ManagedBy=nzshm-backup."""
    all_buckets = [b["Name"] for b in s3_client.list_buckets().get("Buckets", [])]
    managed = []
    for name in all_buckets:
        if old_names_only and not is_old_scheme(name):
            continue
        try:
            tags = s3_client.get_bucket_tagging(Bucket=name)
            tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}
            if tag_dict.get(MANAGED_BY_TAG) == MANAGED_BY_VALUE:
                managed.append(name)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchTagSet", "NoSuchBucket"):
                pass
            else:
                raise
    return managed


def remove_bucket_policy(s3_client, bucket: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] Would remove bucket policy from {bucket}")
        return
    try:
        s3_client.delete_bucket_policy(Bucket=bucket)
        print(f"  Removed bucket policy from {bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
            print(f"  No bucket policy on {bucket} (skipping)")
        else:
            raise


def empty_bucket(s3_client, bucket: str, dry_run: bool) -> int:
    """Delete all objects (and versions) in bucket. Returns object count."""
    count = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        count += len(objects)
        if dry_run:
            print(f"  [dry-run] Would delete {len(objects)} objects from {bucket}")
        else:
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            print(f"  Deleted {len(objects)} objects from {bucket}")

    # Also clean up any delete markers / old versions if versioning was enabled
    try:
        ver_paginator = s3_client.get_paginator("list_object_versions")
        for page in ver_paginator.paginate(Bucket=bucket):
            versions = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if not versions:
                continue
            count += len(versions)
            if dry_run:
                print(f"  [dry-run] Would delete {len(versions)} versions/markers from {bucket}")
            else:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": versions})
                print(f"  Deleted {len(versions)} versions/markers from {bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchBucket":
            raise

    return count


def delete_bucket(s3_client, bucket: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] Would delete bucket {bucket}")
        return
    s3_client.delete_bucket(Bucket=bucket)
    print(f"  Deleted bucket {bucket}")


def confirm(buckets: list[str]) -> bool:
    print(f"\nAbout to permanently delete {len(buckets)} bucket(s):")
    for b in buckets:
        print(f"  - {b}")
    answer = input("\nType 'yes' to confirm: ").strip()
    return answer == "yes"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", default=None, help="AWS profile (e.g. spike-admin)")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--dry-run", action="store_true", help="List buckets without deleting")
    parser.add_argument(
        "--old-names-only",
        action="store_true",
        help="Only delete buckets matching old naming scheme (nzshm-dynamo-backup-* / *-backup-ap-*)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip confirmation prompt (non-interactive use)"
    )
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")

    sts = session.client("sts")
    identity = sts.get_caller_identity()
    print(f"Account: {identity['Account']}  Region: {args.region}")
    if args.profile:
        print(f"Profile: {args.profile}")

    buckets = get_managed_buckets(s3, old_names_only=args.old_names_only)

    if not buckets:
        scope = "old-scheme " if args.old_names_only else ""
        print(f"\nNo {scope}nzshm-backup-managed buckets found.")
        return

    print(f"\nFound {len(buckets)} nzshm-backup-managed bucket(s):")
    for b in buckets:
        print(f"  {b}")

    if args.dry_run:
        print("\n--- DRY RUN — no changes will be made ---")
        for bucket in buckets:
            print(f"\n{bucket}:")
            remove_bucket_policy(s3, bucket, dry_run=True)
            empty_bucket(s3, bucket, dry_run=True)
            delete_bucket(s3, bucket, dry_run=True)
        return

    if not args.yes and not confirm(buckets):
        print("Aborted.")
        sys.exit(0)

    errors = []
    for bucket in buckets:
        print(f"\n{bucket}:")
        try:
            remove_bucket_policy(s3, bucket, dry_run=False)
            empty_bucket(s3, bucket, dry_run=False)
            delete_bucket(s3, bucket, dry_run=False)
        except ClientError as e:
            print(f"  ERROR: {e}")
            errors.append((bucket, str(e)))

    if errors:
        print(f"\n{len(errors)} bucket(s) failed:")
        for b, err in errors:
            print(f"  {b}: {err}")
        sys.exit(1)
    else:
        print(f"\nDone — {len(buckets)} bucket(s) deleted.")


if __name__ == "__main__":
    main()
