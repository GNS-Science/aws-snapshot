#!/usr/bin/env python3
"""Configure S3 Inventory for a backup source (source + backup buckets).

This script sets up daily Parquet inventories for one configured source and
delivers inventory files to a dedicated control bucket in the backup account.

Usage:
    uv run python scripts/setup-inventory.py \
        --config backup-config.production.yaml \
        --source ths \
        --source-profile nshm-admin \
        --backup-profile nshm-backup-admin
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import boto3
import yaml
from botocore.exceptions import ClientError


def _require_account_id_from_lambda_arn(lambda_arn: str) -> str:
    parts = lambda_arn.split(":")
    if len(parts) < 6 or not parts[4].isdigit():
        raise ValueError(f"Cannot derive backup account ID from lambda_arn={lambda_arn!r}")
    return parts[4]


def _sid_for_bucket(bucket: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", bucket)
    return f"AllowInventoryFrom{clean}"[:128]


def _merge_bucket_policy_statement(s3_client, bucket: str, sid: str, statement: dict) -> None:
    try:
        existing = s3_client.get_bucket_policy(Bucket=bucket)
        policy = json.loads(existing["Policy"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
            policy = {"Version": "2012-10-17", "Statement": []}
        else:
            raise

    policy["Statement"] = [s for s in policy["Statement"] if s.get("Sid") != sid]
    policy["Statement"].append(statement)
    s3_client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))


def _ensure_bucket_exists(s3_client, bucket: str, region: str) -> None:
    try:
        s3_client.head_bucket(Bucket=bucket)
        return
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise

    params = {"Bucket": bucket}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3_client.create_bucket(**params)


def _put_inventory(
    s3_client,
    bucket: str,
    inventory_id: str,
    destination_bucket: str,
    destination_account_id: str,
    destination_prefix: str,
) -> None:
    s3_client.put_bucket_inventory_configuration(
        Bucket=bucket,
        Id=inventory_id,
        InventoryConfiguration={
            "Destination": {
                "S3BucketDestination": {
                    "AccountId": destination_account_id,
                    "Bucket": f"arn:aws:s3:::{destination_bucket}",
                    "Format": "Parquet",
                    "Prefix": destination_prefix,
                }
            },
            "IsEnabled": True,
            "Id": inventory_id,
            "IncludedObjectVersions": "Current",
            "OptionalFields": ["Size", "LastModifiedDate", "ETag"],
            "Schedule": {"Frequency": "Daily"},
        },
    )


def _backup_bucket_name(
    source_key: str, bucket_label: str, region: str, source_account_id: str
) -> str:
    return f"bb-{source_key}-s3-{bucket_label}-{region}-{source_account_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to backup config yaml")
    parser.add_argument("--source", required=True, help="Source alias in config")
    parser.add_argument("--source-profile", required=True, help="AWS profile for source account")
    parser.add_argument("--backup-profile", required=True, help="AWS profile for backup account")
    parser.add_argument(
        "--control-bucket",
        default=None,
        help="Inventory destination bucket (default: nzshm-backup-inventory-<backup-account-id>)",
    )
    parser.add_argument(
        "--control-prefix",
        default="inventory",
        help="Inventory destination root prefix in control bucket",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    region = cfg.get("general", {}).get("region", "ap-southeast-2")
    lambda_arn = cfg.get("general", {}).get("lambda_arn", "")
    backup_account_id = _require_account_id_from_lambda_arn(lambda_arn)

    sources = cfg.get("sources", {})
    if args.source not in sources:
        print(f"ERROR: source '{args.source}' not found", file=sys.stderr)
        return 1

    source_cfg = sources[args.source]
    source_account_id = source_cfg.get("source_account_id")
    if not source_account_id:
        print("ERROR: source_account_id is required for inventory setup", file=sys.stderr)
        return 1

    s3_entries = source_cfg.get("s3_buckets", [])
    if not s3_entries:
        print(f"ERROR: source '{args.source}' has no s3_buckets configured", file=sys.stderr)
        return 1

    control_bucket = args.control_bucket or f"nzshm-backup-inventory-{backup_account_id}"

    source_sess = boto3.Session(profile_name=args.source_profile, region_name=region)
    backup_sess = boto3.Session(profile_name=args.backup_profile, region_name=region)
    src_s3 = source_sess.client("s3")
    bkp_s3 = backup_sess.client("s3")

    print(f"Region: {region}")
    print(f"Source account: {source_account_id}  Backup account: {backup_account_id}")
    print(f"Source alias: {args.source}")
    print(f"Control bucket: {control_bucket}")

    if args.dry_run:
        print("[DRY RUN] Would ensure control bucket exists and apply bucket policy statements")
    else:
        _ensure_bucket_exists(bkp_s3, control_bucket, region)

    for entry in s3_entries:
        source_bucket = entry["arn"].split(":::")[-1]
        label = entry["label"]
        backup_bucket = _backup_bucket_name(args.source, label, region, source_account_id)

        src_prefix = f"{args.control_prefix}/{args.source}/source/{source_bucket}"
        bkp_prefix = f"{args.control_prefix}/{args.source}/backup/{backup_bucket}"

        print(f"\nSource bucket: {source_bucket}")
        print(f"Backup bucket: {backup_bucket}")

        for inv_source_bucket, inv_source_account, inv_prefix in [
            (source_bucket, source_account_id, src_prefix),
            (backup_bucket, backup_account_id, bkp_prefix),
        ]:
            sid = _sid_for_bucket(inv_source_bucket)
            statement = {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{control_bucket}/{inv_prefix}/*",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": inv_source_account,
                        "s3:x-amz-acl": "bucket-owner-full-control",
                    },
                    "ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{inv_source_bucket}"},
                },
            }
            if args.dry_run:
                print(
                    f"  [DRY RUN] Would allow inventory writes from {inv_source_bucket} "
                    f"to s3://{control_bucket}/{inv_prefix}/"
                )
            else:
                _merge_bucket_policy_statement(bkp_s3, control_bucket, sid, statement)

        src_inventory_id = f"nzshm-{args.source}-src-{label}"[:64]
        bkp_inventory_id = f"nzshm-{args.source}-bkp-{label}"[:64]

        if args.dry_run:
            print(
                f"  [DRY RUN] Would set source inventory id={src_inventory_id} prefix={src_prefix}"
            )
            print(
                f"  [DRY RUN] Would set backup inventory id={bkp_inventory_id} prefix={bkp_prefix}"
            )
            continue

        _put_inventory(
            src_s3,
            source_bucket,
            src_inventory_id,
            control_bucket,
            backup_account_id,
            src_prefix,
        )
        _put_inventory(
            bkp_s3,
            backup_bucket,
            bkp_inventory_id,
            control_bucket,
            backup_account_id,
            bkp_prefix,
        )
        print(f"  Enabled source inventory: {source_bucket} -> s3://{control_bucket}/{src_prefix}/")
        print(f"  Enabled backup inventory: {backup_bucket} -> s3://{control_bucket}/{bkp_prefix}/")

    print("\nInventory setup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
