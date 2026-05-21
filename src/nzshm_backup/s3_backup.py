"""S3 backup operations module."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of S3 sync operation."""

    objects_copied: int = 0
    bytes_transferred: int = 0
    objects_skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    dry_run: bool = False

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or datetime.now(timezone.utc)
        return (end - self.start_time).total_seconds()

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


@dataclass
class LifecycleConfig:
    """S3 lifecycle policy configuration.

    AWS constraint: DEEP_ARCHIVE transition must be >= hot_days + 90.
    Default warm_days=120 satisfies this for hot_days=30.
    """

    hot_days: int = 30
    warm_days: int = 120
    max_age_days: int = 365
    version_retention_days: int = 365  # how long superseded object versions are kept; 0 = forever


def get_cross_account_session(session: boto3.Session, role_arn: str) -> boto3.Session:
    """Return a new session by assuming a role in another account.

    Args:
        session:  caller's session (must have sts:AssumeRole permission)
        role_arn: ARN of the IAM role to assume in the source account

    Returns:
        New boto3.Session authenticated as the assumed role
    """
    sts = session.client("sts")
    creds = sts.assume_role(
        RoleArn=role_arn, RoleSessionName="nzshm-backup", ExternalId="nzshm-backup"
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=session.region_name,
    )


def get_account_id(session: boto3.Session) -> str:
    """Get AWS account ID from session."""
    sts = session.client("sts")
    return str(sts.get_caller_identity()["Account"])


def get_region(session: boto3.Session) -> str:
    """Get AWS region from session."""
    region = session.region_name
    return region if region else "ap-southeast-2"


def bucket_exists(s3_client, bucket: str) -> bool:
    """Check if S3 bucket exists.

    A 403 response means the bucket exists but the caller lacks s3:ListBucket on it —
    common when checking a restore-target bucket via a cross-account role that has
    s3:CreateBucket but not yet s3:ListBucket (role policy not yet updated).  Treat
    403 as "exists" so the caller skips creation and proceeds to the policy apply step.
    """
    try:
        s3_client.head_bucket(Bucket=bucket)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "404":
            return False
        if code == "403":
            # Bucket exists; caller lacks HeadBucket permission.
            return True
        raise


def bucket_is_ours(s3_client, bucket: str) -> bool:
    """Check if a bucket was created by this tool (has ManagedBy: nzshm-backup tag)."""
    try:
        tags = s3_client.get_bucket_tagging(Bucket=bucket)
        tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}
        return tag_dict.get("ManagedBy") == "nzshm-backup"
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchTagSet":
            return False
        raise


def create_backup_bucket(
    s3_client,
    bucket_name: str,
    region: str,
    account_id: str,
    source_bucket: str = "",
) -> None:
    """Create backup bucket with error handling.

    Args:
        s3_client:     boto3 S3 client
        bucket_name:   Name of backup bucket to create
        region:        AWS region
        account_id:    AWS account ID of the backup account
        source_bucket: Original source bucket name (recorded as a tag)

    Raises:
        ValueError: If bucket already exists
        ClientError: If bucket creation fails
    """
    if bucket_exists(s3_client, bucket_name):
        raise ValueError(f"Backup bucket {bucket_name} already exists - ABEND")

    logger.info(f"Creating backup bucket: {bucket_name}")

    create_bucket_config: dict[str, Any] = {
        "Bucket": bucket_name,
        "ACL": "private",
    }

    if region != "us-east-1":
        create_bucket_config = {
            "Bucket": bucket_name,
            "ACL": "private",
            "CreateBucketConfiguration": {"LocationConstraint": region},
        }

    s3_client.create_bucket(**create_bucket_config)

    tag_set = [
        {"Key": "ManagedBy", "Value": "nzshm-backup"},
        {"Key": "Type", "Value": "backup"},
        {"Key": "Account", "Value": account_id},
    ]
    if source_bucket:
        tag_set.append({"Key": "SourceBucket", "Value": source_bucket})

    s3_client.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tag_set})

    logger.info(f"Created backup bucket: {bucket_name}")


def apply_lifecycle_policy(
    s3_client,
    bucket_name: str,
    config: LifecycleConfig | None = None,
) -> None:
    """Apply lifecycle policy to backup bucket.

    Args:
        s3_client: boto3 S3 client
        bucket_name: Name of bucket
        config: Lifecycle configuration (uses defaults if not provided)
    """
    if config is None:
        config = LifecycleConfig()

    # AWS requires DEEP_ARCHIVE to be at least 90 days after GLACIER_IR
    deep_archive_days = max(config.warm_days, config.hot_days + 90)

    rule: dict = {
        "ID": "BackupTierTransition",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "Transitions": [
            {
                "Days": config.hot_days,
                "StorageClass": "GLACIER_IR",
            },
            {
                "Days": deep_archive_days,
                "StorageClass": "DEEP_ARCHIVE",
            },
        ],
        "Expiration": {
            "Days": config.max_age_days,
        },
    }

    # version_retention_days=0 means retain superseded versions forever (no expiry rule)
    if config.version_retention_days > 0:
        rule["NoncurrentVersionExpiration"] = {"NoncurrentDays": config.version_retention_days}

    logger.info(f"Applying lifecycle policy to {bucket_name}")
    s3_client.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={"Rules": [rule]},
    )


def apply_no_delete_policy(s3_client, bucket_name: str) -> None:
    """Apply bucket policy that denies delete operations.

    This prevents accidental deletion by the backup Lambda.
    Lifecycle expiration still works.

    Args:
        s3_client: boto3 S3 client
        bucket_name: Name of bucket
    """
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyDeleteExceptLifecycle",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:DeleteObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
                "Condition": {"StringNotLike": {"aws:userid": "*s3-lifecycle*"}},
            }
        ],
    }

    logger.info(f"Applying no-delete policy to {bucket_name}")
    s3_client.put_bucket_policy(
        Bucket=bucket_name,
        Policy=json.dumps(policy),
    )


def enable_versioning(s3_client, bucket_name: str) -> None:
    """Enable versioning on a backup bucket.

    Versioning protects against backup poisoning: if a source object is mutated
    and the next sync overwrites the backup copy, the previous (good) version is
    retained as a non-current version and can be restored. Non-current versions
    are expired by the lifecycle policy after ``version_retention_days`` days
    (or kept forever if that value is 0).

    Args:
        s3_client:   boto3 S3 client
        bucket_name: Name of bucket to enable versioning on
    """
    try:
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        logger.info(f"Enabled versioning on {bucket_name}")
    except ClientError as e:
        code = e.response["Error"].get("Code", "Unknown")
        if code == "AccessDenied":
            raise RuntimeError(
                "Failed to enable versioning on backup bucket "
                f"{bucket_name}: missing s3:PutBucketVersioning permission. "
                "Grant this permission to the backup execution role, then run: "
                f"aws s3api put-bucket-versioning --bucket {bucket_name} "
                "--versioning-configuration Status=Enabled"
            ) from e
        raise RuntimeError(
            f"Failed to enable versioning on backup bucket {bucket_name}: {code}"
        ) from e


def sync_bucket(
    s3_client,
    source_bucket: str,
    dest_bucket: str,
    dry_run: bool = False,
    full_sync: bool = False,
    source_s3_client=None,
) -> SyncResult:
    """Sync source bucket to backup bucket (incremental, no delete propagation).

    Args:
        s3_client:        boto3 S3 client for the backup (destination) account
        source_bucket:    Source bucket name
        dest_bucket:      Destination backup bucket name
        dry_run:          If True, only simulate operations
        full_sync:        If True, copy all objects regardless of ETag
        source_s3_client: boto3 S3 client for the source account (cross-account).
                          If None, s3_client is used for both source and dest.

    Returns:
        SyncResult with operation statistics
    """
    src_client = source_s3_client if source_s3_client is not None else s3_client
    result = SyncResult(dry_run=dry_run)

    logger.info(f"Syncing {source_bucket} → {dest_bucket} (dry_run={dry_run})")

    source_paginator = src_client.get_paginator("list_objects_v2")
    source_objects = {}

    for page in source_paginator.paginate(Bucket=source_bucket):
        for obj in page.get("Contents", []):
            source_objects[obj["Key"]] = obj

    dest_objects = {}
    if not dry_run or bucket_exists(s3_client, dest_bucket):
        dest_paginator = s3_client.get_paginator("list_objects_v2")
        for page in dest_paginator.paginate(Bucket=dest_bucket):
            for obj in page.get("Contents", []):
                dest_objects[obj["Key"]] = obj

    for key, source_obj in source_objects.items():
        dest_obj = dest_objects.get(key)

        if dest_obj is None or full_sync:
            should_copy = True
        else:
            should_copy = (
                source_obj["ETag"] != dest_obj["ETag"] or source_obj["Size"] != dest_obj["Size"]
            )

        if should_copy:
            if dry_run:
                logger.debug(f"Would copy: {key} ({source_obj['Size']} bytes)")
                result.objects_copied += 1
                result.bytes_transferred += source_obj["Size"]
            else:
                try:
                    if source_s3_client is not None:
                        # Cross-account: download from source, upload to dest
                        obj_data = src_client.get_object(Bucket=source_bucket, Key=key)
                        s3_client.put_object(
                            Bucket=dest_bucket,
                            Key=key,
                            Body=obj_data["Body"].read(),
                            ContentType=obj_data.get("ContentType", "application/octet-stream"),
                        )
                    else:
                        s3_client.copy_object(
                            CopySource={"Bucket": source_bucket, "Key": key},
                            Bucket=dest_bucket,
                            Key=key,
                            MetadataDirective="COPY",
                        )
                    result.objects_copied += 1
                    result.bytes_transferred += source_obj["Size"]
                    logger.debug(f"Copied: {key}")
                except ClientError as e:
                    result.errors.append(
                        {
                            "key": key,
                            "error": str(e),
                            "operation": "copy",
                        }
                    )
                    logger.error(f"Failed to copy {key}: {e}")
        else:
            result.objects_skipped += 1
            logger.debug(f"Skipped (unchanged): {key}")

    result.end_time = datetime.now(timezone.utc)

    logger.info(
        f"Sync complete: {result.objects_copied} copied, "
        f"{result.objects_skipped} skipped, {result.bytes_transferred} bytes"
    )

    return result


def ensure_backup_bucket_ready(
    session: boto3.Session,
    backup_bucket_name: str,
    lifecycle_config: LifecycleConfig | None = None,
    source_bucket: str = "",
) -> None:
    """Ensure backup bucket exists with proper configuration.

    Args:
        session: boto3 session
        backup_bucket_name: Name of backup bucket
        lifecycle_config: Lifecycle policy configuration

    Raises:
        ValueError: If bucket already exists (ABEND condition)
    """
    s3_client = session.client("s3")
    region = get_region(session)
    account_id = get_account_id(session)

    if not bucket_exists(s3_client, backup_bucket_name):
        create_backup_bucket(s3_client, backup_bucket_name, region, account_id, source_bucket)
        enable_versioning(s3_client, backup_bucket_name)
        apply_lifecycle_policy(s3_client, backup_bucket_name, lifecycle_config)
        apply_no_delete_policy(s3_client, backup_bucket_name)
    elif bucket_is_ours(s3_client, backup_bucket_name):
        logger.info(
            f"Backup bucket {backup_bucket_name} already exists"
            " (managed by nzshm-backup), proceeding"
        )
    else:
        msg = (
            f"Backup bucket {backup_bucket_name} already exists"
            " but is not managed by nzshm-backup - ABEND"
        )
        logger.warning(msg)
        raise ValueError(msg)


def backup_source(
    session: boto3.Session,
    source_bucket: str,
    backup_bucket_name: str,
    dry_run: bool = False,
    full_sync: bool = False,
    source_session: boto3.Session | None = None,
) -> SyncResult:
    """Execute backup for a single S3 bucket.

    Args:
        session:            boto3 session for the backup (destination) account
        source_bucket:      Source bucket ARN or name
        backup_bucket_name: Destination backup bucket name
        dry_run:            Simulate without executing
        full_sync:          Force full copy
        source_session:     boto3 session for the source account (cross-account).
                            If None, session is used for both.

    Returns:
        SyncResult with operation statistics
    """
    dest_s3_client = session.client("s3")
    src_session = source_session if source_session is not None else session
    src_s3_client = src_session.client("s3")

    source_bucket_name = source_bucket.split(":")[-1] if ":" in source_bucket else source_bucket

    if not bucket_exists(src_s3_client, source_bucket_name):
        raise ValueError(f"Source bucket {source_bucket_name} does not exist")

    if not dry_run:
        ensure_backup_bucket_ready(session, backup_bucket_name, source_bucket=source_bucket_name)

    return sync_bucket(
        dest_s3_client,
        source_bucket_name,
        backup_bucket_name,
        dry_run=dry_run,
        full_sync=full_sync,
        source_s3_client=src_s3_client if source_session is not None else None,
    )
