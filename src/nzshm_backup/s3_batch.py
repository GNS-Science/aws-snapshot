"""S3 Batch Operations module for large-bucket backups.

Uses s3control:CreateJob to copy objects asynchronously, avoiding Lambda
timeout on first-run syncs of multi-million-object buckets.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Literal

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.s3_backup import ensure_backup_bucket_ready, get_account_id, get_region

logger = logging.getLogger(__name__)

# Multipart upload threshold: 8 MB
_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MULTIPART_CHUNK = 8 * 1024 * 1024


@dataclass
class BatchJobResult:
    """Result of an S3 Batch Operations job submission."""

    source_bucket: str
    dest_bucket: str
    job_id: str | None
    manifest_key: str
    objects_in_manifest: int
    status: Literal["SUBMITTED", "SKIPPED", "FAILED"]
    errors: list[dict] = field(default_factory=list)
    dry_run: bool = False


def build_manifest_csv(
    source_objects: dict,
    dest_objects: dict,
    source_bucket: str,
    full_sync: bool = False,
) -> Iterator[str]:
    """Yield CSV rows for objects that need copying (new or changed).

    Each row is a single line: ``bucket,key``

    Args:
        source_objects: {key: obj_dict} from list_objects_v2 on source
        dest_objects:   {key: obj_dict} from list_objects_v2 on backup
        source_bucket:  source bucket name (used in CSV rows)
        full_sync:      if True, include all source objects regardless of ETag
    """
    for key, source_obj in source_objects.items():
        dest_obj = dest_objects.get(key)
        if dest_obj is None or full_sync:
            should_copy = True
        else:
            should_copy = (
                source_obj["ETag"] != dest_obj["ETag"] or source_obj["Size"] != dest_obj["Size"]
            )
        if should_copy:
            # Escape key for CSV: wrap in quotes, double any internal quotes
            safe_key = key.replace('"', '""')
            yield f"{source_bucket},{safe_key}\n"


def write_manifest_to_s3(
    s3_client,
    rows: Iterator[str],
    backup_bucket: str,
    manifest_key: str,
) -> tuple[str, int]:
    """Stream manifest rows to S3 via multipart upload.

    Args:
        s3_client:      boto3 S3 client
        rows:           iterator of CSV row strings
        backup_bucket:  destination bucket for the manifest
        manifest_key:   S3 key under which to store the manifest

    Returns:
        (etag, row_count) — ETag required by s3control:CreateJob
    """
    mpu = s3_client.create_multipart_upload(
        Bucket=backup_bucket,
        Key=manifest_key,
        ContentType="text/csv",
    )
    upload_id = mpu["UploadId"]
    parts = []
    part_number = 1
    buffer = b""
    row_count = 0

    try:
        for row in rows:
            buffer += row.encode()
            row_count += 1
            if len(buffer) >= _MULTIPART_CHUNK:
                resp = s3_client.upload_part(
                    Bucket=backup_bucket,
                    Key=manifest_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=buffer,
                )
                parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
                part_number += 1
                buffer = b""

        # Upload final (possibly only) part — multipart requires at least 1 part
        resp = s3_client.upload_part(
            Bucket=backup_bucket,
            Key=manifest_key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=buffer,
        )
        parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})

        complete = s3_client.complete_multipart_upload(
            Bucket=backup_bucket,
            Key=manifest_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return complete["ETag"], row_count

    except Exception:
        s3_client.abort_multipart_upload(
            Bucket=backup_bucket,
            Key=manifest_key,
            UploadId=upload_id,
        )
        raise


def _list_bucket(s3_client, bucket: str) -> dict:
    """Return {key: obj_dict} for all objects in bucket."""
    objects = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = obj
    return objects


def batch_backup_source(
    session: boto3.Session,
    source_bucket: str,
    backup_bucket: str,
    batch_role_arn: str,
    account_id: str,
    dry_run: bool = False,
    full_sync: bool = False,
) -> BatchJobResult:
    """Submit an S3 Batch Operations job to copy new/changed objects.

    Builds a diff manifest CSV, uploads it to ``backup_bucket/_manifests/``,
    then calls s3control:CreateJob.  If the manifest is empty (nothing to copy)
    returns status=SKIPPED without creating a job.

    Args:
        session:         boto3 session
        source_bucket:   source bucket name (not ARN)
        backup_bucket:   destination backup bucket name
        batch_role_arn:  IAM role ARN that S3 Batch will assume
        account_id:      AWS account ID (for s3control API call)
        dry_run:         if True, build manifest but skip CreateJob
        full_sync:       if True, include all objects regardless of ETag
    """
    s3_client = session.client("s3")
    manifest_key = f"_manifests/{source_bucket}-{uuid.uuid4()}.csv"

    if not dry_run:
        ensure_backup_bucket_ready(session, backup_bucket)

    logger.info(f"Listing source objects in {source_bucket}")
    source_objects = _list_bucket(s3_client, source_bucket)
    logger.info(f"Found {len(source_objects)} source objects")

    dest_objects: dict = {}
    try:
        dest_objects = _list_bucket(s3_client, backup_bucket)
        logger.info(f"Found {len(dest_objects)} existing backup objects")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchBucket", "404"):
            logger.info("Backup bucket does not yet exist — all objects will be copied")
        else:
            raise

    rows = build_manifest_csv(source_objects, dest_objects, source_bucket, full_sync)

    if dry_run:
        # Count rows without writing to S3
        row_count = sum(1 for _ in rows)
        logger.info(f"[DRY RUN] Manifest would contain {row_count} objects")
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="SKIPPED",
            dry_run=True,
        )

    logger.info(f"Writing manifest to s3://{backup_bucket}/{manifest_key}")
    manifest_etag, row_count = write_manifest_to_s3(s3_client, rows, backup_bucket, manifest_key)
    logger.info(f"Manifest written: {row_count} objects, ETag={manifest_etag}")

    if row_count == 0:
        logger.info("Nothing to copy — manifest is empty, skipping job submission")
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=0,
            status="SKIPPED",
            dry_run=False,
        )

    region = get_region(session)
    dest_bucket_arn = f"arn:aws:s3:::{backup_bucket}"

    s3control = session.client("s3control", region_name=region)
    try:
        response = s3control.create_job(
            AccountId=account_id,
            ConfirmationRequired=False,
            Operation={
                "S3PutObjectCopy": {
                    "TargetResource": dest_bucket_arn,
                    "MetadataDirective": "COPY",
                    "StorageClass": "STANDARD",
                }
            },
            Manifest={
                "Spec": {
                    "Format": "S3BatchOperations_CSV_20180820",
                    "Fields": ["Bucket", "Key"],
                },
                "Location": {
                    "ObjectArn": f"arn:aws:s3:::{backup_bucket}/{manifest_key}",
                    "ETag": manifest_etag,
                },
            },
            Report={
                "Bucket": dest_bucket_arn,
                "Format": "Report_CSV_20180820",
                "Enabled": True,
                "Prefix": "_batch-reports",
                "ReportScope": "FailedTasksOnly",
            },
            Priority=10,
            RoleArn=batch_role_arn,
            ClientRequestToken=str(uuid.uuid4()),
            Description=f"nzshm-backup: {source_bucket} → {backup_bucket}",
        )
        job_id = response["JobId"]
        logger.info(f"Batch job submitted: {job_id} ({row_count} objects)")
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=job_id,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="SUBMITTED",
            dry_run=False,
        )

    except ClientError as e:
        logger.error(f"Failed to create batch job: {e}")
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="FAILED",
            errors=[{"error": str(e)}],
            dry_run=False,
        )
