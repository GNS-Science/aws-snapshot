#!/usr/bin/env python3
"""One-time setup: create the IAM role S3 Batch Operations assumes.

Account context:
    Run this while authenticated to the BACKUP account. The role allows S3 Batch
    Operations to copy objects from source buckets to backup buckets.

Usage:
    python scripts/create-batch-role.py [--backup-bucket-pattern 'nzshm22-toshi-api-*']

After running:
    Copy the printed ARN into backup-config.yaml:
        general:
          s3_batch_role_arn: "arn:aws:iam::ACCOUNT_ID:role/nzshm-backup-batch-role"
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

ROLE_NAME = "nzshm-backup-batch-role"

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "batchoperations.s3.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def build_permission_policy(account_id: str, region: str, source_bucket_pattern: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadSource",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectTagging"],
                "Resource": f"arn:aws:s3:::{source_bucket_pattern}/*",
            },
            {
                "Sid": "WriteBackup",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:PutObjectTagging",
                    "s3:GetBucketLocation",
                ],
                "Resource": [
                    f"arn:aws:s3:::{source_bucket_pattern}-backup-{region}-{account_id}",
                    f"arn:aws:s3:::{source_bucket_pattern}-backup-{region}-{account_id}/*",
                ],
            },
            {
                "Sid": "ReadManifest",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::*-backup-{region}-{account_id}/_manifests/*",
                ],
            },
            {
                "Sid": "WriteReport",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::*-backup-{region}-{account_id}/_batch-reports/*",
                ],
            },
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-bucket-pattern",
        default="nzshm22-toshi-api-*",
        help="Glob pattern for source buckets (used in IAM resource ARNs)",
    )
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making API calls",
    )
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    iam = session.client("iam")
    sts = session.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    print(f"Account: {account_id}  Region: {args.region}")

    permission_policy = build_permission_policy(
        account_id, args.region, args.source_bucket_pattern
    )

    if args.dry_run:
        print(f"\n[DRY RUN] Would create role: {ROLE_NAME}")
        print("\nTrust policy:")
        print(json.dumps(TRUST_POLICY, indent=2))
        print("\nPermission policy:")
        print(json.dumps(permission_policy, indent=2))
        return

    # Create role
    try:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Role assumed by S3 Batch Operations for nzshm-backup copy jobs",
            Tags=[
                {"Key": "ManagedBy", "Value": "nzshm-backup"},
                {"Key": "Project", "Value": "NSHM"},
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

    # Put inline permission policy
    policy_name = "nzshm-backup-batch-permissions"
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(permission_policy),
    )
    print(f"Attached inline policy: {policy_name}")

    print(f"\nAdd to backup-config.yaml:")
    print(f"  general:")
    print(f"    s3_batch_role_arn: \"{role_arn}\"")


if __name__ == "__main__":
    main()
