"""Per-backup-bucket run state stored as _state/last-run.json.

Provides a lightweight record of the last S3 backup attempt for each
backup bucket — timestamp, phase, and batch job ID. Written during and
at the end of S3 backup runs. Read by the status command to show run
progress before and after S3 Batch submission.

The state object lives at:
    s3://<backup-bucket>/_state/last-run.json

It is operational metadata, not source data. The _state/ prefix follows
the same convention as _manifests/ and _batch-reports/.

Current persisted phases include: running, prepared, submitted, skipped,
completed, failed. Some views may also derive "active" from S3 Batch job
status once a batch_job_id exists.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

STATE_KEY = "_state/last-run.json"


def write_run_state(
    session: boto3.Session,
    backup_bucket: str,
    source_bucket: str,
    status: Literal["running", "prepared", "submitted", "skipped", "completed", "failed"],
    objects_copied: int = 0,
    bytes_transferred: int = 0,
    batch_job_id: str | None = None,
    objects_in_manifest: int = 0,
) -> None:
    """Write last-run state to _state/last-run.json in the backup bucket."""
    state = {
        "source_bucket": source_bucket,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "objects_copied": objects_copied,
        "bytes_transferred": bytes_transferred,
        "batch_job_id": batch_job_id,
        "objects_in_manifest": objects_in_manifest,
    }
    try:
        s3 = session.client("s3")
        s3.put_object(
            Bucket=backup_bucket,
            Key=STATE_KEY,
            Body=json.dumps(state, indent=2).encode(),
            ContentType="application/json",
        )
        logger.debug(f"Wrote run state to s3://{backup_bucket}/{STATE_KEY}")
    except ClientError as e:
        # Non-fatal — state write failure should not fail the backup run
        logger.warning(f"Failed to write run state to {backup_bucket}: {e}")


def read_run_state(session: boto3.Session, backup_bucket: str) -> dict | None:
    """Read last-run state from _state/last-run.json. Returns None if not found."""
    try:
        s3 = session.client("s3")
        resp = s3.get_object(Bucket=backup_bucket, Key=STATE_KEY)
        return dict(json.loads(resp["Body"].read()))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return None
        raise
