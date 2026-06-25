"""DynamoDB Point-in-Time Export backup operations module."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.s3_backup import apply_lifecycle_policy, bucket_exists, get_account_id, get_region

if TYPE_CHECKING:
    # mypy_boto3_s3 is pulled in transitively via aws-sam-cli (dev dep).
    # Imported only under TYPE_CHECKING so runtime doesn't depend on it.
    from mypy_boto3_s3.type_defs import TagTypeDef

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
    s3_bucket_owner: str = "",
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
        export_kwargs: dict[str, Any] = {
            "TableArn": table_arn,
            "S3Bucket": export_bucket,
            "S3Prefix": s3_prefix,
            "ExportFormat": export_format,
        }
        if s3_bucket_owner:
            export_kwargs["S3BucketOwner"] = s3_bucket_owner

        response = dynamodb_client.export_table_to_point_in_time(**export_kwargs)
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
    source_alias: str = "",
    source_account_id: str = "",
) -> None:
    """Ensure DynamoDB export bucket exists with proper configuration.

    Idempotent: if the bucket already exists, log and return (no error).

    Args:
        session:           boto3 session (backup account)
        bucket_name:       Name of DynamoDB export bucket
        source_alias:      Config source alias (e.g. 'arkivalist') recorded as a tag
        source_account_id: Source AWS account ID. If different from the backup account,
                           a bucket policy is added granting the source account IAM root
                           s3:PutObject (DynamoDB cross-account exports write using the
                           calling IAM role's credentials, not the service principal).
    """
    s3_client = session.client("s3")
    region = get_region(session)
    account_id = get_account_id(session)

    bucket_already_existed = bucket_exists(s3_client, bucket_name)

    if not bucket_already_existed:
        logger.info(f"Creating DynamoDB export bucket: {bucket_name}")

        create_bucket_config: dict[str, Any] = {"Bucket": bucket_name, "ACL": "private"}
        if region != "us-east-1":
            create_bucket_config["CreateBucketConfiguration"] = {"LocationConstraint": region}

        s3_client.create_bucket(**create_bucket_config)

        tag_set: list[TagTypeDef] = [
            {"Key": "ManagedBy", "Value": "nzshm-backup"},
            {"Key": "Type", "Value": "dynamodb-export"},
            {"Key": "Account", "Value": account_id},
        ]
        if source_alias:
            tag_set.append({"Key": "SourceAlias", "Value": source_alias})

        s3_client.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tag_set})
        apply_lifecycle_policy(s3_client, bucket_name)
        logger.info(f"Created DynamoDB export bucket: {bucket_name}")
    else:
        logger.info(f"DynamoDB export bucket {bucket_name} already exists")

    if source_account_id and source_account_id != account_id:
        # DynamoDB cross-account exports write to S3 using the CALLING IAM role's credentials
        # (not the dynamodb.amazonaws.com service principal). The bucket policy must grant
        # the source account IAM root access; the reader role's identity policy already
        # scopes this to bb-* buckets in the backup account via s3:ResourceAccount condition.
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowCrossAccountDynamoDBExport",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{source_account_id}:root"},
                    "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                    "Resource": f"arn:aws:s3:::{bucket_name}/*",
                }
            ],
        }
        s3_client.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
        logger.info(
            f"Applied cross-account export bucket policy for source account {source_account_id}"
        )
