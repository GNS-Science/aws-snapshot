"""DynamoDB Point-in-Time Export backup operations module."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.s3_backup import apply_lifecycle_policy, bucket_exists, get_account_id, get_region

logger = logging.getLogger(__name__)

utc = timezone.utc


@dataclass
class ExportResult:
    """Result of a DynamoDB Point-in-Time export operation."""

    table_name: str
    table_arn: str
    export_arn: str | None
    export_bucket: str
    export_prefix: str
    status: Literal["INITIATED", "SKIPPED", "FAILED"]
    errors: list[dict[str, Any]] = field(default_factory=list)
    start_time: datetime = field(default_factory=lambda: datetime.now(utc))
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return self.status != "FAILED" and not self.errors


def export_dynamodb_table(
    dynamodb_client,
    table_arn: str,
    export_bucket: str,
    export_format: Literal["DYNAMODB_JSON", "ION"] = "DYNAMODB_JSON",
    dry_run: bool = False,
) -> ExportResult:
    """Export a DynamoDB table to S3 using Point-in-Time Recovery.

    Args:
        dynamodb_client: boto3 DynamoDB client
        table_arn: ARN of the DynamoDB table to export
        export_bucket: S3 bucket name for export destination
        export_format: Export format (DYNAMODB_JSON or ION)
        dry_run: If True, only simulate the export

    Returns:
        ExportResult with operation status
    """
    table_name = table_arn.split("/")[-1]
    s3_prefix = f"dynamodb-exports/{table_name}/{datetime.now(utc).strftime('%Y/%m/%d')}"

    result = ExportResult(
        table_name=table_name,
        table_arn=table_arn,
        export_arn=None,
        export_bucket=export_bucket,
        export_prefix=s3_prefix,
        status="SKIPPED",
        dry_run=dry_run,
    )

    if dry_run:
        logger.info(
            f"[DRY RUN] Would export {table_name} → s3://{export_bucket}/{s3_prefix} "
            f"(format={export_format})"
        )
        return result

    try:
        response = dynamodb_client.export_table_to_point_in_time(
            TableArn=table_arn,
            S3Bucket=export_bucket,
            S3Prefix=s3_prefix,
            ExportFormat=export_format,
        )
        result.export_arn = response["ExportDescription"]["ExportArn"]
        result.status = "INITIATED"
        logger.info(f"Export initiated: {table_name} → {result.export_arn}")
    except ClientError as e:
        result.errors.append({"table_arn": table_arn, "error": str(e), "operation": "export"})
        result.status = "FAILED"
        logger.error(f"Failed to export {table_name}: {e}")

    return result


def ensure_dynamodb_backup_bucket_ready(
    session: boto3.Session,
    bucket_name: str,
) -> None:
    """Ensure DynamoDB export bucket exists with proper configuration.

    Idempotent: if the bucket already exists, log and return (no error).

    Args:
        session: boto3 session
        bucket_name: Name of DynamoDB export bucket
    """
    s3_client = session.client("s3")
    region = get_region(session)
    account_id = get_account_id(session)

    if bucket_exists(s3_client, bucket_name):
        logger.info(f"DynamoDB export bucket {bucket_name} already exists, skipping creation")
        return

    logger.info(f"Creating DynamoDB export bucket: {bucket_name}")

    create_bucket_config: dict[str, Any] = {"Bucket": bucket_name, "ACL": "private"}
    if region != "us-east-1":
        create_bucket_config["CreateBucketConfiguration"] = {"LocationConstraint": region}

    s3_client.create_bucket(**create_bucket_config)

    s3_client.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={
            "TagSet": [
                {"Key": "ManagedBy", "Value": "nzshm-backup"},
                {"Key": "Type", "Value": "dynamodb-export"},
                {"Key": "Account", "Value": account_id},
            ]
        },
    )

    apply_lifecycle_policy(s3_client, bucket_name)

    logger.info(f"Created DynamoDB export bucket: {bucket_name}")
