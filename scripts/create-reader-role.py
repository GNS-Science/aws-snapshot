#!/usr/bin/env python3
"""One-time setup: create the IAM reader role in a SOURCE account.

This role is assumed by the backup Lambda (running in the BACKUP account)
to read S3 buckets and initiate DynamoDB exports cross-account.

Account context:
    Run this while authenticated to the SOURCE account (e.g. Arkivalist 816711409078).

Usage (config-driven — recommended):
    # Derives everything from the config file; writes source_account_role_arn back.
    python scripts/create-reader-role.py \
        --config backup-config.sandbox.yaml \
        --source arkivalist

    # Dry-run first to preview what will be created:
    python scripts/create-reader-role.py \
        --config backup-config.sandbox.yaml \
        --source arkivalist \
        --dry-run

Usage (explicit — for scripting or when config is unavailable):
    python scripts/create-reader-role.py \
        --backup-account-id 595842668254 \
        --s3-buckets arkivalist-api-dev-serverlessdeploymentbucket-oztlskap4vrh \
        --dynamodb-tables arkivalist-api-dev-events arkivalist-api-dev-feedback \
            arkivalist-api-dev-invite-codes arkivalist-api-dev-mission-events \
            arkivalist-api-dev-mission-runs

    # With S3 Batch support (required when use_s3_batch: true):
    python scripts/create-reader-role.py \
        --backup-account-id 595842668254 \
        --batch-role-arn arn:aws:iam::595842668254:role/nzshm-backup-batch-role \
        --s3-buckets nzshm-toshi-api-data \
        --dynamodb-tables ToshiFileObject-PROD ...

After running (explicit mode only):
    Copy the printed ARN into backup-config.yaml under the source:
        sources:
          arkivalist:
            source_account_role_arn: "arn:aws:iam::816711409078:role/nzshm-backup-reader"

    Config-driven mode writes this back automatically.
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml
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


def build_permission_policy(
    region: str,
    account_id: str,
    s3_buckets: list[str],
    dynamodb_tables: list[str],
    backup_account_id: str = "",
) -> dict:
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
            "Resource": [
                f"arn:aws:dynamodb:{region}:{account_id}:table/{t}"
                for t in dynamodb_tables
            ],
        })
        statements.append({
            "Sid": "DescribeExport",
            "Effect": "Allow",
            "Action": ["dynamodb:DescribeExport"],
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
        # pitr-watcher: re-enable PITR on restored tables and remove PITRPending tag.
        # Scoped to all tables in the account because restored table names (e.g.
        # <original>-restored) are not in the configured table list.
        statements.append({
            "Sid": "PITRWatcherReEnableOnRestoredTables",
            "Effect": "Allow",
            "Action": [
                "dynamodb:UpdateContinuousBackups",
                "dynamodb:UntagResource",
                "dynamodb:DescribeTable",
            ],
            "Resource": [f"arn:aws:dynamodb:{region}:{account_id}:table/*"],
        })
        statements.append({
            "Sid": "PITRWatcherTagScan",
            "Effect": "Allow",
            "Action": ["tag:GetResources"],
            "Resource": ["*"],
        })

    return {"Version": "2012-10-17", "Statement": statements}


def apply_batch_role_bucket_policy(
    s3_client, bucket: str, batch_role_arn: str, dry_run: bool
) -> None:
    """Add a bucket policy statement allowing the batch role to read source objects.

    S3 Batch Operations copies using the batch role's credentials. For cross-account
    source buckets the batch role (in the backup account) needs both an identity policy
    (already in create-batch-role.py) AND a resource policy on the source bucket.
    """
    if dry_run:
        print(f"  [dry-run] Would add batch role read policy to {bucket}")
        return

    sid = "AllowNzshmBatchRoleRead"
    try:
        existing = s3_client.get_bucket_policy(Bucket=bucket)
        policy = json.loads(existing["Policy"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
            policy = {"Version": "2012-10-17", "Statement": []}
        else:
            raise

    policy["Statement"] = [s for s in policy["Statement"] if s.get("Sid") != sid]
    policy["Statement"].append({
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"AWS": batch_role_arn},
        "Action": ["s3:GetObject", "s3:GetObjectTagging"],
        "Resource": f"arn:aws:s3:::{bucket}/*",
    })

    s3_client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print(f"  Added batch role read policy to bucket: {bucket}")


def write_back_role_arn(config_path: str, source_alias: str, role_arn: str) -> None:
    """Update source_account_role_arn in the config YAML file."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    data["sources"][source_alias]["source_account_role_arn"] = role_arn
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"  Updated {config_path}: sources.{source_alias}.source_account_role_arn = {role_arn}")


def resolve_from_config(config_path: str, source_alias: str, batch_role_arn_override: str | None):
    """Load config and return (backup_account_id, region, s3_buckets, dynamodb_tables, batch_role_arn)."""
    with open(config_path) as f:
        data = yaml.safe_load(f)

    general = data.get("general", {})
    sources = data.get("sources", {})

    if source_alias not in sources:
        print(f"ERROR: source '{source_alias}' not found in {config_path}.", file=sys.stderr)
        print(f"  Available sources: {', '.join(sources)}", file=sys.stderr)
        sys.exit(1)

    source = sources[source_alias]

    # Derive backup account ID from lambda_arn: arn:aws:lambda:{region}:{account_id}:...
    lambda_arn = general.get("lambda_arn", "")
    parts = lambda_arn.split(":")
    if len(parts) < 6 or not parts[4].isdigit():
        print(
            f"ERROR: cannot derive backup account ID from general.lambda_arn={lambda_arn!r}.\n"
            "  Use --backup-account-id to provide it explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)
    backup_account_id = parts[4]
    region = general.get("region", "ap-southeast-2")

    s3_buckets = [b["arn"].split(":::")[-1] for b in source.get("s3_buckets", [])]
    dynamodb_tables = [arn.split("/")[-1] for arn in source.get("dynamodb_tables", [])]

    # Batch role: explicit override > config (when use_s3_batch: true)
    batch_role_arn = batch_role_arn_override
    if batch_role_arn is None and source.get("use_s3_batch"):
        batch_role_arn = general.get("s3_batch_role_arn")

    return backup_account_id, region, s3_buckets, dynamodb_tables, batch_role_arn


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Config-driven mode
    config_group = parser.add_argument_group("config-driven mode (recommended)")
    config_group.add_argument("--config", default=None, help="Path to backup config YAML file")
    config_group.add_argument("--source", default=None, help="Source alias from config (e.g. arkivalist)")

    # Explicit mode
    explicit_group = parser.add_argument_group("explicit mode")
    explicit_group.add_argument("--backup-account-id", default=None, help="Account ID that runs the backup Lambda")
    explicit_group.add_argument("--s3-buckets", nargs="*", default=[], help="Source S3 bucket names (not ARNs)")
    explicit_group.add_argument("--dynamodb-tables", nargs="*", default=[], help="Source DynamoDB table names (not ARNs)")
    explicit_group.add_argument("--region", default="ap-southeast-2")

    # Shared
    parser.add_argument(
        "--batch-role-arn", default=None,
        help="Override batch role ARN. In config mode, auto-derived from general.s3_batch_role_arn "
             "when use_s3_batch: true.",
    )
    parser.add_argument(
        "--profile", default=None,
        help="AWS profile (SOURCE account). NOTE: SSO requires eval first: "
             "eval $(aws configure export-credentials --profile <profile> --format env)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without making API calls")
    args = parser.parse_args()

    # Determine mode
    config_mode = args.config is not None and args.source is not None
    explicit_mode = args.backup_account_id is not None

    if config_mode and explicit_mode:
        parser.error("Use either --config/--source OR --backup-account-id, not both.")
    if not config_mode and not explicit_mode:
        parser.error("Provide either --config + --source, or --backup-account-id.")

    if config_mode:
        backup_account_id, region, s3_buckets, dynamodb_tables, batch_role_arn = resolve_from_config(
            args.config, args.source, args.batch_role_arn
        )
    else:
        backup_account_id = args.backup_account_id
        region = args.region
        s3_buckets = args.s3_buckets
        dynamodb_tables = args.dynamodb_tables
        batch_role_arn = args.batch_role_arn

    session = boto3.Session(profile_name=args.profile, region_name=region)
    iam = session.client("iam")
    s3 = session.client("s3")
    sts = session.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    print(f"Source account: {account_id}  Backup account: {backup_account_id}  Region: {region}")
    if config_mode:
        print(f"Config: {args.config}  Source: {args.source}")
    print(f"S3 buckets: {s3_buckets or '(none)'}")
    print(f"DynamoDB tables: {dynamodb_tables or '(none)'}")
    if batch_role_arn:
        print(f"Batch role: {batch_role_arn}")

    trust_policy = build_trust_policy(backup_account_id)
    permission_policy = build_permission_policy(
        region, account_id, s3_buckets, dynamodb_tables, backup_account_id
    )

    if args.dry_run:
        print(f"\n[DRY RUN] Would create/update role: {ROLE_NAME}")
        print("\nTrust policy:")
        print(json.dumps(trust_policy, indent=2))
        print("\nPermission policy:")
        print(json.dumps(permission_policy, indent=2))
        if batch_role_arn and s3_buckets:
            print(f"\nBatch role bucket policies ({batch_role_arn}):")
            for bucket in s3_buckets:
                apply_batch_role_bucket_policy(s3, bucket, batch_role_arn, dry_run=True)
        if config_mode:
            role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
            print(f"\n[dry-run] Would update {args.config}: "
                  f"sources.{args.source}.source_account_role_arn = {role_arn}")
        return

    # Create or update role
    try:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Read-only role assumed by nzshm-backup Lambda for cross-account backup",
            Tags=[{"Key": "ManagedBy", "Value": "nzshm-backup"}],
        )
        role_arn = resp["Role"]["Arn"]
        print(f"\nCreated role: {role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
            print(f"\nRole already exists: {role_arn}")
        else:
            print(f"ERROR creating role: {e}", file=sys.stderr)
            sys.exit(1)

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="nzshm-backup-reader-permissions",
        PolicyDocument=json.dumps(permission_policy),
    )
    print("Attached inline policy: nzshm-backup-reader-permissions")

    if batch_role_arn and s3_buckets:
        print(f"\nApplying batch role bucket policies:")
        for bucket in s3_buckets:
            apply_batch_role_bucket_policy(s3, bucket, batch_role_arn, dry_run=False)

    if config_mode:
        print(f"\nWriting role ARN back to config:")
        write_back_role_arn(args.config, args.source, role_arn)
    else:
        print(f"\nAdd to backup-config.yaml under the relevant source:")
        print(f"    source_account_role_arn: \"{role_arn}\"")


if __name__ == "__main__":
    main()
