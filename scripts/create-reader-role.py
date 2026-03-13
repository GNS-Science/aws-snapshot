#!/usr/bin/env python3
"""One-time setup: create the IAM reader role in a source account.

This role is assumed by the backup Lambda (running in the backup account)
to read S3 buckets and initiate DynamoDB exports cross-account.

Usage:
    # Run while authenticated to the SOURCE account (e.g. Arkivalist 816711409078)
    python scripts/create-reader-role.py \
        --backup-account-id 595842668254 \
        --s3-buckets arkivalist-api-dev-serverlessdeploymentbucket-oztlskap4vrh \
        --dynamodb-tables arkivalist-api-dev-events arkivalist-api-dev-feedback \
            arkivalist-api-dev-invite-codes arkivalist-api-dev-mission-events \
            arkivalist-api-dev-mission-runs

After running, copy the printed ARN into backup-config.yaml under the source:
    sources:
      arkivalist:
        source_account_role_arn: "arn:aws:iam::816711409078:role/nzshm-backup-reader"
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

ROLE_NAME = "nzshm-backup-reader"


def build_trust_policy(backup_account_id: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{backup_account_id}:root"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"sts:ExternalId": "nzshm-backup"}
                },
            }
        ],
    }


def build_permission_policy(region: str, account_id: str, s3_buckets: list[str], dynamodb_tables: list[str], backup_account_id: str = "") -> dict:
    statements = []

    if s3_buckets:
        statements.append({
            "Sid": "ListSourceBuckets",
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": [f"arn:aws:s3:::{b}" for b in s3_buckets],
            "Condition": {"StringEquals": {"s3:ResourceAccount": account_id}},
        })
        statements.append({
            "Sid": "ReadSourceObjects",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectTagging"],
            "Resource": [f"arn:aws:s3:::{b}/*" for b in s3_buckets],
            "Condition": {"StringEquals": {"s3:ResourceAccount": account_id}},
        })

    if dynamodb_tables:
        statements.append({
            "Sid": "ExportDynamoDBTables",
            "Effect": "Allow",
            "Action": [
                "dynamodb:ExportTableToPointInTime",
                "dynamodb:DescribeContinuousBackups",
            ],
            "Resource": [
                f"arn:aws:dynamodb:{region}:{account_id}:table/{t}"
                for t in dynamodb_tables
            ],
        })
        statements.append({
            "Sid": "ListExports",
            "Effect": "Allow",
            "Action": ["dynamodb:ListExports"],
            # ListExports is scoped to the table ARN
            "Resource": [
                f"arn:aws:dynamodb:{region}:{account_id}:table/{t}"
                for t in dynamodb_tables
            ],
        })
        statements.append({
            "Sid": "DescribeExport",
            "Effect": "Allow",
            "Action": ["dynamodb:DescribeExport"],
            # DescribeExport is scoped to the export ARN (table/*/export/*)
            "Resource": [
                f"arn:aws:dynamodb:{region}:{account_id}:table/{t}/export/*"
                for t in dynamodb_tables
            ],
        })
        if backup_account_id:
            statements.append({
                "Sid": "WriteDynamoDBExportBucket",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [f"arn:aws:s3:::bb-*-{region}-{account_id}/*"],
                "Condition": {"StringEquals": {"s3:ResourceAccount": backup_account_id}},
            })

    return {"Version": "2012-10-17", "Statement": statements}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-account-id", required=True, help="Account ID that runs the backup Lambda")
    parser.add_argument("--s3-buckets", nargs="*", default=[], help="Source S3 bucket names (not ARNs)")
    parser.add_argument("--dynamodb-tables", nargs="*", default=[], help="Source DynamoDB table names (not ARNs)")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--profile", default=None, help="AWS profile name. NOTE: SSO profiles require eval first: eval $(aws configure export-credentials --profile <profile> --format env)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be created without API calls")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    iam = session.client("iam")
    sts = session.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    print(f"Source account: {account_id}  Backup account: {args.backup_account_id}  Region: {args.region}")

    trust_policy = build_trust_policy(args.backup_account_id)
    permission_policy = build_permission_policy(args.region, account_id, args.s3_buckets, args.dynamodb_tables, args.backup_account_id)

    if args.dry_run:
        print(f"\n[DRY RUN] Would create role: {ROLE_NAME}")
        print("\nTrust policy:")
        print(json.dumps(trust_policy, indent=2))
        print("\nPermission policy:")
        print(json.dumps(permission_policy, indent=2))
        return

    try:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Read-only role assumed by nzshm-backup Lambda for cross-account backup",
            Tags=[
                {"Key": "ManagedBy", "Value": "nzshm-backup"},
            ],
        )
        role_arn = resp["Role"]["Arn"]
        print(f"Created role: {role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
            print(f"Role already exists: {role_arn}")
        else:
            print(f"ERROR creating role: {e}", file=sys.stderr)
            sys.exit(1)

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="nzshm-backup-reader-permissions",
        PolicyDocument=json.dumps(permission_policy),
    )
    print("Attached inline policy: nzshm-backup-reader-permissions")

    print(f"\nAdd to backup-config.yaml under the relevant source:")
    print(f"    source_account_role_arn: \"{role_arn}\"")


if __name__ == "__main__":
    main()
