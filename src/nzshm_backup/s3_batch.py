"""S3 Batch Operations module for large-bucket backups.

Uses s3control:CreateJob to copy objects asynchronously, avoiding Lambda
timeout on first-run syncs of multi-million-object buckets.
"""

import csv
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.integrity import OPERATIONAL_PREFIXES
from nzshm_backup.inventory_state import _expected_prefix
from nzshm_backup.s3_backup import ensure_backup_bucket_ready, get_region

logger = logging.getLogger(__name__)

# Multipart upload threshold: 8 MB
_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MULTIPART_CHUNK = 8 * 1024 * 1024
_INVENTORY_DT_RE = re.compile(r"/hive/dt=([^/]+)/")


@dataclass
class BatchJobResult:
    """Result of an S3 Batch Operations job submission."""

    source_bucket: str
    dest_bucket: str
    job_id: str | None
    manifest_key: str
    objects_in_manifest: int
    status: Literal["SUBMITTED", "PREPARED", "SKIPPED", "FAILED"]
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
            # S3 Batch CSV manifests require URL-encoded keys. Keep path separators.
            safe_key = quote(key, safe="/")
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


def _normalize_etag(value: str) -> str:
    return value.strip().strip('"')


def _latest_inventory_parquet_keys(
    s3_client,
    control_bucket: str,
    inventory_prefix: str,
) -> tuple[str, list[str]]:
    snapshots: dict[str, list[str]] = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    prefix = f"{inventory_prefix.rstrip('/')}/hive/"
    for page in paginator.paginate(Bucket=control_bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            if not key.endswith(".parquet"):
                continue
            match = _INVENTORY_DT_RE.search(key)
            if not match:
                continue
            snapshots.setdefault(match.group(1), []).append(key)

    if not snapshots:
        raise ValueError(f"No inventory parquet data found under s3://{control_bucket}/{prefix}")

    latest_dt = max(snapshots.keys())
    return latest_dt, sorted(snapshots[latest_dt])


def _iter_inventory_rows(
    s3_client,
    control_bucket: str,
    parquet_keys: list[str],
) -> Iterator[tuple[str, int, str]]:
    expression = 'SELECT s."key", s."size", s."e_tag" FROM S3Object s'
    for parquet_key in parquet_keys:
        response = s3_client.select_object_content(
            Bucket=control_bucket,
            Key=parquet_key,
            ExpressionType="SQL",
            Expression=expression,
            InputSerialization={"Parquet": {}},
            OutputSerialization={"CSV": {}},
        )

        pending = ""
        for event in response["Payload"]:
            records = event.get("Records")
            if not records:
                continue
            pending += records["Payload"].decode("utf-8")
            lines = pending.splitlines(keepends=False)
            if pending and not pending.endswith("\n"):
                pending = lines.pop() if lines else pending
            else:
                pending = ""
            for line in lines:
                if not line:
                    continue
                cols = next(csv.reader([line]))
                if len(cols) < 3:
                    continue
                key = cols[0]
                size = int(cols[1]) if cols[1] else 0
                etag = _normalize_etag(cols[2])
                yield key, size, etag

        if pending:
            cols = next(csv.reader([pending]))
            if len(cols) >= 3:
                key = cols[0]
                size = int(cols[1]) if cols[1] else 0
                etag = _normalize_etag(cols[2])
                yield key, size, etag


def _build_manifest_rows_from_inventory(
    session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
    full_sync: bool,
) -> tuple[Iterator[str], str, str]:
    backup_s3 = session.client("s3")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"

    src_prefix = _expected_prefix(source_alias, "source", source_bucket)
    bkp_prefix = _expected_prefix(source_alias, "backup", backup_bucket)

    src_dt, src_keys = _latest_inventory_parquet_keys(backup_s3, control_bucket, src_prefix)
    bkp_dt, bkp_keys = _latest_inventory_parquet_keys(backup_s3, control_bucket, bkp_prefix)

    backup_objects: dict[str, tuple[str, int]] = {}
    if not full_sync:
        for key, size, etag in _iter_inventory_rows(backup_s3, control_bucket, bkp_keys):
            if any(key.startswith(p) for p in OPERATIONAL_PREFIXES):
                continue
            backup_objects[key] = (etag, size)

    def _rows() -> Iterator[str]:
        for key, size, etag in _iter_inventory_rows(backup_s3, control_bucket, src_keys):
            current = backup_objects.get(key)
            if full_sync or current is None or current[0] != etag or current[1] != size:
                safe_key = quote(key, safe="/")
                yield f"{source_bucket},{safe_key}\n"

    logger.info(
        f"Using inventory snapshots source={src_dt} backup={bkp_dt} "
        f"from s3://{control_bucket}/{src_prefix} and s3://{control_bucket}/{bkp_prefix}"
    )
    return _rows(), src_dt, bkp_dt


def batch_backup_source(
    session: boto3.Session,
    source_bucket: str,
    backup_bucket: str,
    batch_role_arn: str,
    account_id: str,
    dry_run: bool = False,
    full_sync: bool = False,
    source_session: boto3.Session | None = None,
    prepare_only: bool = False,
    source_alias: str | None = None,
    manifest_mode: Literal["inline", "inventory"] = "inline",
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
        source_alias:    source key in config (required for inventory mode)
        manifest_mode:   'inline' (live list/diff) or 'inventory' (latest inventory snapshot diff)
    """
    s3_client = session.client("s3")
    src_s3_client = source_session.client("s3") if source_session is not None else s3_client
    manifest_key = f"_manifests/{source_bucket}-{uuid.uuid4()}.csv"

    if dry_run:
        # Skip full object enumeration for dry-run — listing millions of objects just to
        # count them is slow and not representative of the real run (which delegates listing
        # to AWS S3 Batch). Instead, confirm read access with a single list page.
        logger.info(f"[DRY RUN] Would submit S3 Batch job: {source_bucket} → {backup_bucket}")
        try:
            src_s3_client.list_objects_v2(Bucket=source_bucket, MaxKeys=1)
            logger.info(f"[DRY RUN] Access check passed: {source_bucket} is readable")
        except ClientError as e:
            logger.error(f"[DRY RUN] Access check failed for {source_bucket}: {e}")
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=-1,  # -1 = not enumerated in dry-run
            status="SKIPPED",
            dry_run=True,
        )

    ensure_backup_bucket_ready(session, backup_bucket)

    if manifest_mode == "inventory":
        if not source_alias:
            raise ValueError("source_alias is required when manifest_mode='inventory'")
        rows, src_dt, bkp_dt = _build_manifest_rows_from_inventory(
            session,
            source_alias,
            source_bucket,
            backup_bucket,
            full_sync,
        )
        logger.info(f"Inventory diff snapshots selected: source={src_dt}, backup={bkp_dt}")
    else:
        logger.info(f"Listing source objects in {source_bucket}")
        source_objects = _list_bucket(src_s3_client, source_bucket)
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

    if prepare_only:
        logger.info(
            f"Prepare-only mode: manifest ready at s3://{backup_bucket}/{manifest_key} "
            f"({row_count} objects), skipping S3 Batch submission"
        )
        return BatchJobResult(
            source_bucket=source_bucket,
            dest_bucket=backup_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="PREPARED",
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


def _build_restore_manifest_rows(
    s3_client,
    backup_bucket: str,
    prefix: str | None = None,
) -> Iterator[str]:
    """Yield CSV rows for all restorable objects in a backup bucket.

    Excludes operational prefixes (_manifests/, _batch-reports/, _state/, etc.)
    which are internal metadata and should not be restored to the workload bucket.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    kwargs: dict = {"Bucket": backup_bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if any(key.startswith(p) for p in OPERATIONAL_PREFIXES):
                continue
            safe_key = quote(key, safe="/")
            yield f"{backup_bucket},{safe_key}\n"


def batch_restore_bucket(
    session: boto3.Session,
    backup_bucket: str,
    target_bucket: str,
    batch_role_arn: str,
    account_id: str,
    dry_run: bool = False,
    prefix: str | None = None,
    prebuilt_manifest_key: str | None = None,
    prebuilt_manifest_etag: str | None = None,
    prebuilt_manifest_row_count: int | None = None,
) -> BatchJobResult:
    """Submit an S3 Batch Operations job to restore a bucket from backup.

    Unlike batch_backup_source (which diffs source vs backup), this always copies
    all non-operational objects — restore is a recovery operation where a clean,
    known state is preferred over partial sync semantics.

    The manifest and job report are stored in the backup bucket. The batch role
    must have s3:GetObject on the backup bucket and s3:PutObject on the target.
    For cross-account targets the target bucket policy must also allow the role.

    Args:
        session:                      boto3 Session (backup account).
        backup_bucket:                Backup bucket to restore from.
        target_bucket:                Destination bucket to restore into (must exist).
        batch_role_arn:               IAM role ARN that S3 Batch will assume.
        account_id:                   Backup account ID (for s3control API).
        dry_run:                      If True, build manifest but skip CreateJob.
        prefix:                       Optional key prefix — restores only matching objects.
        prebuilt_manifest_key:        S3 key of a pre-written manifest in backup_bucket.
                                      If provided, skips manifest generation entirely.
        prebuilt_manifest_etag:       ETag of the pre-written manifest (required with
                                      prebuilt_manifest_key).
        prebuilt_manifest_row_count:  Row count for the pre-written manifest (used for
                                      the returned BatchJobResult).

    Returns:
        BatchJobResult with status SUBMITTED, SKIPPED, or FAILED.
    """
    s3_client = session.client("s3")
    region = get_region(session)

    if prebuilt_manifest_key is not None:
        manifest_key = prebuilt_manifest_key
        manifest_etag = prebuilt_manifest_etag
        row_count = prebuilt_manifest_row_count or 0
        if dry_run:
            logger.info(f"[DRY RUN] Using pre-built manifest: {manifest_key} ({row_count} objects)")
            return BatchJobResult(
                source_bucket=backup_bucket,
                dest_bucket=target_bucket,
                job_id=None,
                manifest_key=manifest_key,
                objects_in_manifest=row_count,
                status="SKIPPED",
                dry_run=True,
            )
    else:
        manifest_key = f"_manifests/restore-{uuid.uuid4()}.csv"
        rows = _build_restore_manifest_rows(s3_client, backup_bucket, prefix)

        if dry_run:
            row_count = sum(1 for _ in rows)
            logger.info(f"[DRY RUN] Restore manifest would contain {row_count} objects")
            return BatchJobResult(
                source_bucket=backup_bucket,
                dest_bucket=target_bucket,
                job_id=None,
                manifest_key=manifest_key,
                objects_in_manifest=row_count,
                status="SKIPPED",
                dry_run=True,
            )

        logger.info(f"Writing restore manifest to s3://{backup_bucket}/{manifest_key}")
        manifest_etag, row_count = write_manifest_to_s3(
            s3_client, rows, backup_bucket, manifest_key
        )
    logger.info(f"Restore manifest: {row_count} objects, ETag={manifest_etag}")

    if row_count == 0:
        logger.info("Nothing to restore — manifest is empty")
        return BatchJobResult(
            source_bucket=backup_bucket,
            dest_bucket=target_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=0,
            status="SKIPPED",
        )

    s3control = session.client("s3control", region_name=region)
    try:
        response = s3control.create_job(
            AccountId=account_id,
            ConfirmationRequired=False,
            Operation={
                "S3PutObjectCopy": {
                    "TargetResource": f"arn:aws:s3:::{target_bucket}",
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
                "Bucket": f"arn:aws:s3:::{backup_bucket}",
                "Format": "Report_CSV_20180820",
                "Enabled": True,
                "Prefix": "_batch-reports",
                "ReportScope": "FailedTasksOnly",
            },
            Priority=10,
            RoleArn=batch_role_arn,
            ClientRequestToken=str(uuid.uuid4()),
            Description=f"nzshm-restore: {backup_bucket} → {target_bucket}",
        )
        job_id = response["JobId"]
        logger.info(f"Restore batch job submitted: {job_id} ({row_count} objects)")
        return BatchJobResult(
            source_bucket=backup_bucket,
            dest_bucket=target_bucket,
            job_id=job_id,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="SUBMITTED",
        )
    except ClientError as e:
        logger.error(f"Failed to create restore batch job: {e}")
        return BatchJobResult(
            source_bucket=backup_bucket,
            dest_bucket=target_bucket,
            job_id=None,
            manifest_key=manifest_key,
            objects_in_manifest=row_count,
            status="FAILED",
            errors=[{"error": str(e)}],
        )


def list_recent_batch_jobs(
    s3control_client,
    account_id: str,
    description_contains: str,
    limit: int = 3,
) -> list[dict]:
    """Return recent S3 Batch jobs whose description contains description_contains, newest first.

    Args:
        s3control_client:      boto3 s3control client
        account_id:            AWS account ID
        description_contains:  substring to match against job Description
        limit:                 maximum number of jobs to return
    """
    jobs = []
    kwargs: dict = {"AccountId": account_id, "MaxResults": 25}
    while True:
        response = s3control_client.list_jobs(**kwargs)
        for job in response.get("Jobs", []):
            if description_contains in job.get("Description", ""):
                jobs.append(job)
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    jobs.sort(key=lambda j: j.get("CreationTime") or "", reverse=True)
    return jobs[:limit]


def wait_for_batch_job(
    session: boto3.Session,
    account_id: str,
    job_id: str,
    poll_interval: int = 10,
    timeout: int = 600,
) -> str:
    """Poll s3control:DescribeJob until the job reaches a terminal state.

    Args:
        session:       boto3 Session.
        account_id:    AWS account ID for s3control.
        job_id:        S3 Batch job ID.
        poll_interval: Seconds between polls.
        timeout:       Maximum seconds to wait before raising TimeoutError.

    Returns:
        Final job status string (e.g. ``"Complete"``, ``"Failed"``).

    Raises:
        TimeoutError: If the job does not complete within ``timeout`` seconds.
    """
    import time

    region = get_region(session)
    s3control = session.client("s3control", region_name=region)
    _terminal = {"Complete", "Failed", "Cancelled"}
    elapsed = 0

    while elapsed < timeout:
        resp = s3control.describe_job(AccountId=account_id, JobId=job_id)
        status = resp["Job"]["Status"]
        if status in _terminal:
            logger.info(f"Batch job {job_id} reached terminal state: {status}")
            return str(status)
        logger.info(f"Batch job {job_id}: {status} (elapsed: {elapsed}s)")
        time.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(f"Batch job {job_id} did not complete within {timeout}s")
