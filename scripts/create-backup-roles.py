#!/usr/bin/env python3
"""One-time setup: create IAM roles in the BACKUP account.

Account context:
    Run this while authenticated to the BACKUP account. Creates the role that S3 Batch
    Operations assumes for both backup (source → backup bucket) and restore
    (backup bucket → source) directions.

Usage (config-driven — recommended):
    # Derives source bucket list from config; covers all sources with use_s3_batch: true.
    python scripts/create-backup-roles.py --config backup-config.sandbox.yaml

Usage (explicit):
    python scripts/create-backup-roles.py \
        --source-buckets nzshm-toshi-api-data nzshm22-toshi-api-sandbox

After running:
    Copy the printed ARN into backup-config.yaml:
        general:
          s3_batch_role_arn: "arn:aws:iam::ACCOUNT_ID:role/nzshm-backup-batch-role"

    Config-driven mode writes this back automatically.

Cross-account restore setup:
    For cross-account restores the source bucket (restore target) needs a resource policy
    allowing this role to PutObject. Run create-source-roles.py in the SOURCE account to
    apply both the read policy (backup direction) and write policy (restore direction):

        python scripts/create-source-roles.py --config backup-config.sandbox.yaml --source arkivalist
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml
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


def build_permission_policy(account_id: str, region: str, source_buckets: list[str]) -> dict:
    # Backup buckets follow the bb-* naming convention:
    #   bb-{source}-s3-{label}-{region}-{source-account-id}
    # The source-account-id in the bucket name is the SOURCE account, not the backup account,
    # so we cannot use account_id in the backup bucket ARNs — use bb-* wildcard instead.
    read_source_resources = (
        [f"arn:aws:s3:::{b}/*" for b in source_buckets]
        if source_buckets
        else ["arn:aws:s3:::*/*"]  # fallback — all buckets; source bucket policy gates access
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadSource",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectTagging"],
                "Resource": read_source_resources,
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
                    f"arn:aws:s3:::bb-*-{region}-*",
                    f"arn:aws:s3:::bb-*-{region}-*/*",
                ],
            },
            {
                "Sid": "ReadBackup",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectTagging"],
                "Resource": [f"arn:aws:s3:::bb-*-{region}-*/*"],
            },
            {
                "Sid": "WriteRestore",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:PutObjectTagging",
                    "s3:GetBucketLocation",
                ],
                # Covers both the original bucket (real DR) and the {bucket}-restore default
                # target (safe testing). For cross-account targets the bucket policy must also
                # allow this role (applied by create-source-roles.py --config <cfg> --source <alias>).
                "Resource": (
                    [f"arn:aws:s3:::{b}/*" for b in source_buckets] +
                    [f"arn:aws:s3:::{b}-restore/*" for b in source_buckets]
                ) if source_buckets else ["arn:aws:s3:::*/*"],
            },
            {
                "Sid": "WriteReport",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [f"arn:aws:s3:::bb-*-{region}-*/_batch-reports/*"],
            },
        ],
    }


def resolve_from_config(config_path: str) -> tuple[str, list[str]]:
    """Return (region, source_buckets) for all sources with use_s3_batch: true."""
    with open(config_path) as f:
        data = yaml.safe_load(f)

    region = data.get("general", {}).get("region", "ap-southeast-2")
    source_buckets = []
    for alias, source in data.get("sources", {}).items():
        if source.get("use_s3_batch"):
            for b in source.get("s3_buckets", []):
                bucket_name = b["arn"].split(":::")[-1]
                source_buckets.append(bucket_name)
    return region, source_buckets


def write_back_role_arn(config_path: str, role_arn: str) -> None:
    """Update general.s3_batch_role_arn in the config YAML file."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    data.setdefault("general", {})["s3_batch_role_arn"] = role_arn
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"  Updated {config_path}: general.s3_batch_role_arn = {role_arn}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=None, help="Path to backup config YAML (config-driven mode)")
    parser.add_argument(
        "--source-buckets", nargs="*", default=[],
        help="Explicit source bucket names (not ARNs). Ignored when --config is used.",
    )
    parser.add_argument("--region", default="ap-southeast-2", help="AWS region (explicit mode only)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be created without API calls")
    args = parser.parse_args()

    config_mode = args.config is not None

    if config_mode:
        region, source_buckets = resolve_from_config(args.config)
        if not source_buckets:
            print(
                "WARNING: no sources have use_s3_batch: true in config. "
                "ReadSource will allow all buckets (source bucket policies gate access)."
            )
    else:
        region = args.region
        source_buckets = args.source_buckets

    session = boto3.Session(region_name=region)
    iam = session.client("iam")
    sts = session.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    print(f"Account: {account_id}  Region: {region}")
    if config_mode:
        print(f"Config: {args.config}")
    print(f"Source buckets: {source_buckets or '(all — wildcard fallback)'}")

    permission_policy = build_permission_policy(account_id, region, source_buckets)

    if args.dry_run:
        print(f"\n[DRY RUN] Would create/update role: {ROLE_NAME}")
        print("\nTrust policy:")
        print(json.dumps(TRUST_POLICY, indent=2))
        print("\nPermission policy:")
        print(json.dumps(permission_policy, indent=2))
        return

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
        print(f"\nCreated role: {role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
            print(f"\nRole already exists: {role_arn}")
        else:
            print(f"ERROR creating role: {e}", file=sys.stderr)
            sys.exit(1)

    policy_name = "nzshm-backup-batch-permissions"
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(permission_policy),
    )
    print(f"Attached inline policy: {policy_name}")

    if config_mode:
        print(f"\nWriting role ARN back to config:")
        write_back_role_arn(args.config, role_arn)
    else:
        print(f"\nAdd to backup-config.yaml:")
        print(f"  general:")
        print(f"    s3_batch_role_arn: \"{role_arn}\"")


if __name__ == "__main__":
    main()
