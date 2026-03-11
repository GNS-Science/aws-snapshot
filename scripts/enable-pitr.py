#!/usr/bin/env python3
"""Enable Point-in-Time Recovery (PITR) on one or more DynamoDB tables.

PITR is required before DynamoDB export-to-S3 can be initiated.
Run this while authenticated to the account that owns the tables.

Usage:
    # Enable PITR on tables in the source account (run authenticated to that account)
    python scripts/enable-pitr.py \
        --tables \
            my-table-one \
            my-table-two \
            my-table-three

    # Check current PITR status without changing anything
    python scripts/enable-pitr.py --tables my-table-one --status-only
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def get_pitr_status(client, table_name: str) -> str:
    try:
        resp = client.describe_continuous_backups(TableName=table_name)
        return (
            resp["ContinuousBackupsDescription"]
            ["PointInTimeRecoveryDescription"]
            ["PointInTimeRecoveryStatus"]
        )
    except ClientError as e:
        return f"ERROR: {e}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", nargs="+", required=True, help="DynamoDB table names")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--status-only", action="store_true", help="Report PITR status without making changes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    client = session.client("dynamodb")
    sts = session.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    print(f"Account: {account_id}  Region: {args.region}")
    print()

    errors = []

    for table in args.tables:
        status = get_pitr_status(client, table)

        if args.status_only or args.dry_run:
            print(f"  {table}: PITR={status}")
            continue

        if status == "ENABLED":
            print(f"  {table}: PITR already ENABLED — skipping")
            continue

        try:
            client.update_continuous_backups(
                TableName=table,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
            )
            print(f"  {table}: PITR ENABLED")
        except ClientError as e:
            print(f"  {table}: ERROR — {e}", file=sys.stderr)
            errors.append(table)

    if errors:
        print(f"\nFailed for {len(errors)} table(s): {', '.join(errors)}", file=sys.stderr)
        sys.exit(1)

    if not args.status_only and not args.dry_run:
        print("\nDone. Allow a few minutes for PITR to become fully active before running exports.")


if __name__ == "__main__":
    main()
