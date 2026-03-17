"""S3 restore operations — copy objects from a backup bucket to a target bucket."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.integrity import OPERATIONAL_PREFIXES
from nzshm_backup.s3_backup import bucket_exists, get_account_id, get_region

logger = logging.getLogger(__name__)


@dataclass
class RestoreResult:
    """Result of an S3 restore operation."""

    source_bucket: str
    target_bucket: str
    objects_copied: int = 0
    bytes_transferred: int = 0
    objects_skipped: int = 0  # already present with matching ETag
    errors: list[dict[str, Any]] = field(default_factory=list)
    prefix_filter: str | None = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or datetime.now(timezone.utc)
        return (end - self.start_time).total_seconds()

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def _ensure_restore_target(s3_client, bucket_name: str, region: str) -> None:
    """Create target bucket if it does not exist.

    Unlike ensure_backup_bucket_ready, this does NOT apply lifecycle policies,
    delete-deny bucket policies, or nzshm-backup tags — the target is a workload
    bucket, not a backup bucket.
    """
    if bucket_exists(s3_client, bucket_name):
        logger.info(f"Restore target {bucket_name} already exists, proceeding")
        return

    logger.info(f"Creating restore target bucket: {bucket_name}")
    kwargs: dict = {"Bucket": bucket_name, "ACL": "private"}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3_client.create_bucket(**kwargs)
    s3_client.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={
            "TagSet": [
                {"Key": "RestoredBy", "Value": "nzshm-backup"},
                {"Key": "RestoredAt", "Value": datetime.now(timezone.utc).isoformat()},
            ]
        },
    )


def restore_s3_bucket(
    session: boto3.Session,
    backup_bucket: str,
    target_bucket: str,
    prefix: str | None = None,
) -> RestoreResult:
    """Copy objects from a backup bucket to a target (workload) bucket.

    Objects already present in the target with a matching ETag are skipped
    (incremental restore). Objects in the target that are not in the backup
    are left untouched.

    Args:
        session:       boto3 Session (backup account).
        backup_bucket: Source backup bucket to restore from.
        target_bucket: Destination bucket to restore into.
        prefix:        Optional S3 key prefix — restores only matching objects.

    Returns:
        RestoreResult with per-object statistics.
    """
    s3_client = session.client("s3")
    region = get_region(session)
    result = RestoreResult(
        source_bucket=backup_bucket,
        target_bucket=target_bucket,
        prefix_filter=prefix,
    )

    _ensure_restore_target(s3_client, target_bucket, region)

    # Build index of objects already in target
    target_objects: dict[str, str] = {}  # key → ETag
    paginator = s3_client.get_paginator("list_objects_v2")
    list_kwargs: dict = {"Bucket": target_bucket}
    if prefix:
        list_kwargs["Prefix"] = prefix
    for page in paginator.paginate(**list_kwargs):
        for obj in page.get("Contents", []):
            target_objects[obj["Key"]] = obj["ETag"]

    # Stream objects from backup bucket
    list_kwargs = {"Bucket": backup_bucket}
    if prefix:
        list_kwargs["Prefix"] = prefix
    for page in paginator.paginate(**list_kwargs):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            size = obj["Size"]
            source_etag = obj["ETag"]

            if any(key.startswith(p) for p in OPERATIONAL_PREFIXES):
                logger.debug(f"Skipped (operational metadata): {key}")
                continue

            if target_objects.get(key) == source_etag:
                result.objects_skipped += 1
                logger.debug(f"Skipped (already present): {key}")
                continue

            try:
                s3_client.copy_object(
                    CopySource={"Bucket": backup_bucket, "Key": key},
                    Bucket=target_bucket,
                    Key=key,
                    MetadataDirective="COPY",
                )
                result.objects_copied += 1
                result.bytes_transferred += size
                logger.debug(f"Restored: {key}")
            except ClientError as e:
                logger.error(f"Failed to restore {key}: {e}")
                result.errors.append({"key": key, "error": str(e)})

    result.end_time = datetime.now(timezone.utc)
    logger.info(
        f"Restore complete: {result.objects_copied} copied, "
        f"{result.objects_skipped} skipped, {len(result.errors)} errors"
    )
    return result
