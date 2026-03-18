"""DynamoDB restore operations — PITR restore_table_to_point_in_time."""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

PITR_WATCHER_RULE_NAME = os.environ.get("PITR_WATCHER_RULE_NAME", "nzshm-backup-pitr-watcher")
PITR_PENDING_TAG = "PITRPending"

_MAX_TABLE_NAME_LEN = 255
_RESTORE_SUFFIX = "-restored"


@dataclass
class DynamoDBRestoreResult:
    """Result of submitting a DynamoDB PITR restore."""

    source_table_arn: str
    target_table_name: str
    restore_point: datetime
    restore_arn: str | None = None   # ARN of the new table being created
    status: Literal["INITIATED", "SKIPPED", "FAILED"] = "INITIATED"
    errors: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def success(self) -> bool:
        return self.status in ("INITIATED", "SKIPPED")


@dataclass
class DynamoDBRestoreStatus:
    """Live status of a DynamoDB table restore."""

    table_name: str
    table_status: str                  # e.g. "CREATING", "ACTIVE", "RESTORING"
    restore_in_progress: bool
    restore_date_time: datetime | None  # the point-in-time the restore targets
    source_table_arn: str | None
    restore_status: str | None          # "RESTORING", "SUCCEEDED", "FAILED"


def make_restore_table_name(source_table_arn: str) -> str:
    """Derive a restore target table name from a source ARN.

    Takes the table name from the ARN and appends ``-restored``, truncating
    the base name if necessary to stay within the 255-character DynamoDB limit.

    Args:
        source_table_arn: Full DynamoDB table ARN.

    Returns:
        Target table name with ``-restored`` suffix.
    """
    base_name = source_table_arn.split("/")[-1]
    max_base = _MAX_TABLE_NAME_LEN - len(_RESTORE_SUFFIX)
    if len(base_name) > max_base:
        logger.warning(
            f"Table name {base_name!r} truncated to {max_base} chars to fit suffix"
        )
        base_name = base_name[:max_base]
    return base_name + _RESTORE_SUFFIX


def restore_dynamodb_table(
    dynamodb_client,
    source_table_arn: str,
    target_table_name: str,
    restore_point: datetime,
    dry_run: bool = False,
    enable_pitr: bool = True,
) -> DynamoDBRestoreResult:
    """Submit a DynamoDB PITR restore request.

    This is a submit-and-return operation. The restore runs asynchronously
    inside AWS and typically takes 2–8 hours. Use ``describe_restore_status``
    to poll progress.

    Args:
        dynamodb_client:   boto3 DynamoDB client (source account or backup account
                           — whichever has permission to read the PITR stream).
        source_table_arn:  ARN of the original table to restore from.
        target_table_name: Name for the new restored table.
        restore_point:     Point in time to restore to (must be timezone-aware UTC).
        dry_run:           If True, log intent but make no API calls.
        enable_pitr:       If True (default), tag the restored table with PITRPending=true
                           so the pitr-watcher Lambda re-enables PITR automatically once
                           the table reaches ACTIVE. Pass False only for short-lived test
                           restores that will be deleted immediately.

    Returns:
        DynamoDBRestoreResult with status INITIATED (or SKIPPED for dry run).
    """
    result = DynamoDBRestoreResult(
        source_table_arn=source_table_arn,
        target_table_name=target_table_name,
        restore_point=restore_point,
        dry_run=dry_run,
    )

    if dry_run:
        logger.info(
            f"[DRY RUN] Would restore {source_table_arn} → {target_table_name} "
            f"at {restore_point.isoformat()}"
        )
        result.status = "SKIPPED"
        return result

    try:
        response = dynamodb_client.restore_table_to_point_in_time(
            SourceTableArn=source_table_arn,
            TargetTableName=target_table_name,
            RestoreDateTime=restore_point,
            BillingModeOverride="PAY_PER_REQUEST",
        )
        result.restore_arn = response["TableDescription"]["TableArn"]
        result.status = "INITIATED"
        logger.info(
            f"Restore initiated: {source_table_arn} → {target_table_name} "
            f"({result.restore_arn})"
        )
    except ClientError as e:
        logger.error(f"Restore failed for {source_table_arn}: {e}")
        result.status = "FAILED"
        result.errors.append({"table_arn": source_table_arn, "error": str(e)})
        return result

    # RestoreTableToPointInTime does not accept Tags — apply them separately.
    # The table may not be immediately visible to tag_resource after submission,
    # so retry a few times with a short delay.
    if enable_pitr and result.restore_arn:
        import time

        tags = [
            {"Key": "RestoredBy",    "Value": "nzshm-backup"},
            {"Key": PITR_PENDING_TAG, "Value": "true"},
            {"Key": "RestoredFrom",  "Value": source_table_arn.split("/")[-1]},
            {"Key": "RestoredAt",    "Value": restore_point.isoformat()},
        ]
        # Retry with backoff — the table can take up to ~60s to register after submission.
        for attempt in range(6):
            try:
                dynamodb_client.tag_resource(ResourceArn=result.restore_arn, Tags=tags)
                logger.info(f"Tagged {target_table_name} with PITRPending=true")
                break
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException" and attempt < 5:
                    wait = 2 * (2 ** attempt)  # 2s, 4s, 8s, 16s, 32s
                    logger.info(f"Table not yet visible for tagging, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    # Non-fatal: watcher won't fire automatically, but restore itself succeeded.
                    # Manually tag with: aws dynamodb tag-resource --resource-arn <arn>
                    #   --tags Key=PITRPending,Value=true Key=RestoredBy,Value=nzshm-backup
                    logger.warning(f"Could not tag {target_table_name} for PITR watcher: {e}")
                    break

    return result


def describe_restore_status(
    dynamodb_client,
    table_name: str,
) -> DynamoDBRestoreStatus:
    """Query the current restore status of a DynamoDB table.

    Args:
        dynamodb_client: boto3 DynamoDB client.
        table_name:      Name of the target (restored) table.

    Returns:
        DynamoDBRestoreStatus populated from describe_table.

    Raises:
        ClientError: If the table does not exist or another API error occurs.
    """
    response = dynamodb_client.describe_table(TableName=table_name)
    table = response["Table"]
    summary = table.get("RestoreSummary", {})

    restore_dt = summary.get("RestoreDateTime")
    if restore_dt and restore_dt.tzinfo is None:
        restore_dt = restore_dt.replace(tzinfo=timezone.utc)

    return DynamoDBRestoreStatus(
        table_name=table_name,
        table_status=table.get("TableStatus", "UNKNOWN"),
        restore_in_progress=summary.get("RestoreInProgress", False),
        restore_date_time=restore_dt,
        source_table_arn=summary.get("SourceTableArn"),
        restore_status="RESTORING" if summary.get("RestoreInProgress") else (
            "SUCCEEDED" if summary else None
        ),
    )
