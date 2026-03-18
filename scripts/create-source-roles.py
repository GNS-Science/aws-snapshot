#!/usr/bin/env python3
"""One-time setup: create IAM roles in a SOURCE account for nzshm-backup.

Creates two roles:

  nzshm-backup-reader   — read-only; assumed by the backup Lambda for S3 sync
                          and DynamoDB exports.

  nzshm-backup-restore  — restore operations; assumed by the restore CLI and
                          pitr-watcher Lambda for RestoreTableToPointInTime,
                          PITR re-enable, and tag management.

Account context:
    Run this while authenticated to the SOURCE account (e.g. Arkivalist 456789012345).

Usage (config-driven — recommended):
    python scripts/create-source-roles.py \
        --config backup-config.sandbox.yaml \
        --source arkivalist

    # Dry-run first to preview what will be created:
    python scripts/create-source-roles.py \
        --config backup-config.sandbox.yaml \
        --source arkivalist \
        --dry-run

Usage (explicit — for scripting or when config is unavailable):
    python scripts/create-source-roles.py \
        --backup-account-id 345678901234 \
        --s3-buckets my-source-bucket \
        --dynamodb-tables MyTable-PROD

Config-driven mode writes both role ARNs back to the config file automatically.
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

READER_ROLE_NAME = "nzshm-backup-reader"
RESTORE_ROLE_NAME = "nzshm-backup-restore"


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


def build_reader_policy(
    region: str,
    account_id: str,
    s3_buckets: list[str],
    dynamodb_tables: list[str],
    backup_account_id: str = "",
) -> dict:
    """Read-only policy: S3 source reads + DynamoDB export initiation."""
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

    return {"Version": "2012-10-17", "Statement": statements}


def build_restore_policy(
    region: str,
    account_id: str,
    dynamodb_tables: list[str],
) -> dict:
    """Restore policy: submit PITR restores and manage PITR/tags on restored tables."""
    statements = []

    if dynamodb_tables:
        statements.append({
            "Sid": "SubmitPITRRestore",
            "Effect": "Allow",
            "Action": ["dynamodb:RestoreTableToPointInTime"],
            "Resource": [
                f"arn:aws:dynamodb:{region}:{account_id}:table/{t}"
                for t in dynamodb_tables
            ],
        })
        # Restored table names (e.g. <original>-restored) are not in the configured
        # table list, so these permissions must be scoped to all tables in the account.
        statements.append({
            "Sid": "ManageRestoredTables",
            "Effect": "Allow",
            "Action": [
                "dynamodb:TagResource",
                "dynamodb:UntagResource",
                "dynamodb:DescribeTable",
                "dynamodb:UpdateContinuousBackups",
            ],
            "Resource": [f"arn:aws:dynamodb:{region}:{account_id}:table/*"],
        })
        statements.append({
            "Sid": "PITRPendingTagScan",
            "Effect": "Allow",
            "Action": ["tag:GetResources"],
            "Resource": ["*"],
        })

    return {"Version": "2012-10-17", "Statement": statements}


def _create_or_update_role(iam, role_name: str, trust_policy: dict, permission_policy: dict,
                            description: str, dry_run: bool) -> str:
    """Create or update an IAM role and its inline policy. Returns the role ARN."""
    if dry_run:
        print(f"\n[DRY RUN] Would create/update role: {role_name}")
        print(f"  Description: {description}")
        print(f"  Trust policy:\n{json.dumps(trust_policy, indent=4)}")
        print(f"  Permission policy:\n{json.dumps(permission_policy, indent=4)}")
        # Return a plausible ARN for dry-run write-back previews
        caller = boto3.client("sts").get_caller_identity()
        return f"arn:aws:iam::{caller['Account']}:role/{role_name}"

    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=description,
            Tags=[{"Key": "ManagedBy", "Value": "nzshm-backup"}],
        )
        role_arn = resp["Role"]["Arn"]
        print(f"\nCreated role: {role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
            print(f"\nRole already exists, updating: {role_arn}")
            iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy),
            )
        else:
            print(f"ERROR creating role {role_name}: {e}", file=sys.stderr)
            sys.exit(1)

    policy_name = f"nzshm-backup-{role_name.removeprefix('nzshm-backup-')}-permissions"
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(permission_policy),
    )
    print(f"  Attached inline policy: {policy_name}")
    return role_arn


def apply_batch_role_bucket_policy(
    s3_client, bucket: str, batch_role_arn: str, dry_run: bool
) -> None:
    """Add a bucket policy statement allowing the batch role to read source objects."""
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


def write_back_role_arns(config_path: str, source_alias: str,
                          reader_arn: str, restore_arn: str) -> None:
    """Update both role ARNs in the config YAML file."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    data["sources"][source_alias]["source_account_role_arn"] = reader_arn
    data["sources"][source_alias]["source_account_restore_role_arn"] = restore_arn
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"  Updated {config_path}:")
    print(f"    sources.{source_alias}.source_account_role_arn = {reader_arn}")
    print(f"    sources.{source_alias}.source_account_restore_role_arn = {restore_arn}")


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

    batch_role_arn = batch_role_arn_override
    if batch_role_arn is None and source.get("use_s3_batch"):
        batch_role_arn = general.get("s3_batch_role_arn")

    return backup_account_id, region, s3_buckets, dynamodb_tables, batch_role_arn


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    config_group = parser.add_argument_group("config-driven mode (recommended)")
    config_group.add_argument("--config", default=None, help="Path to backup config YAML file")
    config_group.add_argument("--source", default=None, help="Source alias from config")

    explicit_group = parser.add_argument_group("explicit mode")
    explicit_group.add_argument("--backup-account-id", default=None)
    explicit_group.add_argument("--s3-buckets", nargs="*", default=[])
    explicit_group.add_argument("--dynamodb-tables", nargs="*", default=[])
    explicit_group.add_argument("--region", default="ap-southeast-2")

    parser.add_argument("--batch-role-arn", default=None)
    parser.add_argument(
        "--profile", default=None,
        help="AWS profile (SOURCE account). NOTE: SSO requires eval first: "
             "eval $(aws configure export-credentials --profile <profile> --format env)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
    print(f"S3 buckets:      {s3_buckets or '(none)'}")
    print(f"DynamoDB tables: {dynamodb_tables or '(none)'}")

    trust_policy = build_trust_policy(backup_account_id)
    reader_policy = build_reader_policy(region, account_id, s3_buckets, dynamodb_tables, backup_account_id)
    restore_policy = build_restore_policy(region, account_id, dynamodb_tables)

    reader_arn = _create_or_update_role(
        iam, READER_ROLE_NAME, trust_policy, reader_policy,
        description="Read-only role assumed by nzshm-backup Lambda for S3 backup and DynamoDB exports",
        dry_run=args.dry_run,
    )
    restore_arn = _create_or_update_role(
        iam, RESTORE_ROLE_NAME, trust_policy, restore_policy,
        description="Restore role assumed by nzshm-backup for DynamoDB PITR restore and PITR re-enable",
        dry_run=args.dry_run,
    )

    if batch_role_arn and s3_buckets:
        print(f"\nApplying batch role bucket policies ({batch_role_arn}):")
        for bucket in s3_buckets:
            apply_batch_role_bucket_policy(s3, bucket, batch_role_arn, dry_run=args.dry_run)

    if config_mode:
        print("\nWriting role ARNs back to config:")
        if not args.dry_run:
            write_back_role_arns(args.config, args.source, reader_arn, restore_arn)
        else:
            print(f"  [dry-run] Would set source_account_role_arn = {reader_arn}")
            print(f"  [dry-run] Would set source_account_restore_role_arn = {restore_arn}")
    else:
        print(f"\nAdd to backup-config.yaml under the relevant source:")
        print(f"    source_account_role_arn: \"{reader_arn}\"")
        print(f"    source_account_restore_role_arn: \"{restore_arn}\"")


if __name__ == "__main__":
    main()
