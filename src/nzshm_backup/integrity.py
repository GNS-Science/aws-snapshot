"""Backup integrity checking — source bucket vs backup bucket comparison."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Objects written by the backup tooling itself; no counterpart in source buckets.
OPERATIONAL_PREFIXES = ("_state/", "_manifests/", "_batch-reports/", "_events/")


def _is_operational(key: str) -> bool:
    return any(key.startswith(p) for p in OPERATIONAL_PREFIXES)


@dataclass
class ObjectDiff:
    """A single discrepancy between source and backup."""

    key: str
    issue: Literal["missing_in_backup", "etag_mismatch"]
    source_etag: str | None = None
    backup_etag: str | None = None


@dataclass
class IntegrityResult:
    """Result of comparing a source bucket against its backup."""

    source_bucket: str
    backup_bucket: str
    source_object_count: int = 0  # objects in source (operational prefixes excluded)
    backup_object_count: int = 0  # objects in backup (operational prefixes excluded)
    diffs: list[ObjectDiff] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None

    @property
    def missing_count(self) -> int:
        return sum(1 for d in self.diffs if d.issue == "missing_in_backup")

    @property
    def mismatch_count(self) -> int:
        return sum(1 for d in self.diffs if d.issue == "etag_mismatch")

    @property
    def clean(self) -> bool:
        """True iff no diffs and no errors."""
        return not self.diffs and not self.errors


def check_bucket_integrity(
    backup_s3_client,
    source_bucket: str,
    backup_bucket: str,
    source_s3_client=None,
) -> IntegrityResult:
    """Compare every object in source_bucket against backup_bucket.

    Checks for:
    - Objects present in source but absent from backup (``missing_in_backup``).
    - Objects present in both but with differing ETags (``etag_mismatch``), which
      indicates a source mutation was propagated to the backup by a prior sync run.

    Objects present in backup but absent from source are intentionally NOT flagged
    — the backup retains deleted objects until the lifecycle policy expires them.

    Operational prefixes (``_state/``, ``_manifests/``, ``_batch-reports/``) are
    excluded from both sides before comparison.

    Args:
        backup_s3_client:  boto3 S3 client for the backup account.
        source_bucket:     Name of the original source bucket.
        backup_bucket:     Name of the backup bucket to compare against.
        source_s3_client:  boto3 S3 client for the source account (cross-account).
                           Falls back to ``backup_s3_client`` if None.

    Returns:
        IntegrityResult with diff list and object counts.
    """
    src_client = source_s3_client if source_s3_client is not None else backup_s3_client
    result = IntegrityResult(source_bucket=source_bucket, backup_bucket=backup_bucket)
    paginator = backup_s3_client.get_paginator("list_objects_v2")

    # Index source objects: key → ETag
    source_objects: dict[str, str] = {}
    try:
        src_paginator = src_client.get_paginator("list_objects_v2")
        for page in src_paginator.paginate(Bucket=source_bucket):
            for obj in page.get("Contents", []):
                if not _is_operational(obj["Key"]):
                    source_objects[obj["Key"]] = obj["ETag"]
        result.source_object_count = len(source_objects)
    except Exception as e:
        logger.error(f"Failed to list source bucket {source_bucket}: {e}")
        result.errors.append({"bucket": source_bucket, "error": str(e)})
        result.end_time = datetime.now(timezone.utc)
        return result

    # Index backup objects: key → ETag
    backup_objects: dict[str, str] = {}
    try:
        for page in paginator.paginate(Bucket=backup_bucket):
            for obj in page.get("Contents", []):
                if not _is_operational(obj["Key"]):
                    backup_objects[obj["Key"]] = obj["ETag"]
        result.backup_object_count = len(backup_objects)
    except Exception as e:
        logger.error(f"Failed to list backup bucket {backup_bucket}: {e}")
        result.errors.append({"bucket": backup_bucket, "error": str(e)})
        result.end_time = datetime.now(timezone.utc)
        return result

    # Compare
    for key, source_etag in source_objects.items():
        backup_etag = backup_objects.get(key)
        if backup_etag is None:
            result.diffs.append(
                ObjectDiff(key=key, issue="missing_in_backup", source_etag=source_etag)
            )
        elif backup_etag != source_etag:
            result.diffs.append(
                ObjectDiff(
                    key=key, issue="etag_mismatch", source_etag=source_etag, backup_etag=backup_etag
                )
            )

    result.end_time = datetime.now(timezone.utc)
    logger.info(
        f"Integrity check: {result.source_object_count} source objects, "
        f"{result.backup_object_count} backup objects, "
        f"{result.missing_count} missing, {result.mismatch_count} mismatched"
    )
    return result
