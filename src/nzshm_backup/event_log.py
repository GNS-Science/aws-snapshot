"""Append-only JSONL event log stored in backup buckets under _events/YYYY-MM/events.jsonl.

Provides a durable audit trail of backup and restore operations. Follows the
existing _state/ and _manifests/ prefix convention inside each backup bucket.

Non-fatal: write failures log a warning but never fail the calling operation.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_EVENTS_PREFIX = "_events"


def _event_key(dt: datetime) -> str:
    return f"{_EVENTS_PREFIX}/{dt.strftime('%Y-%m')}/events.jsonl"


def append_event(
    session,
    backup_bucket: str,
    event_type: str,
    source: str,
    details: dict[str, Any],
    actor: str | None = None,
) -> None:
    """Append one event to the monthly JSONL log in the backup bucket.

    Non-fatal: logs a warning on failure rather than raising.
    """
    now = datetime.now(timezone.utc)
    event: dict[str, Any] = {
        "event_type": event_type,
        "source": source,
        "timestamp": now.isoformat(),
        "details": details,
    }
    if actor:
        event["actor"] = actor

    key = _event_key(now)
    s3 = session.client("s3")

    try:
        try:
            resp = s3.get_object(Bucket=backup_bucket, Key=key)
            existing = resp["Body"].read().decode()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
                existing = ""
            else:
                raise

        new_content = (existing.rstrip("\n") + "\n" + json.dumps(event) + "\n").lstrip("\n")
        s3.put_object(
            Bucket=backup_bucket,
            Key=key,
            Body=new_content.encode(),
            ContentType="application/x-ndjson",
        )
        logger.debug(f"Appended {event_type} event to s3://{backup_bucket}/{key}")
    except Exception as e:
        logger.warning(f"Failed to write event log to {backup_bucket}: {e}")


def read_events(
    session,
    backup_bucket: str,
    source: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read events from the JSONL log. Returns most recent first.

    Scans the current and previous month by default; scans from *since* month
    onwards when provided.
    """
    s3 = session.client("s3")
    now = datetime.now(timezone.utc)

    # Build list of month start datetimes to scan
    if since:
        months: list[datetime] = []
        dt = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while dt <= now:
            months.append(dt)
            dt = dt.replace(month=dt.month + 1) if dt.month < 12 else dt.replace(year=dt.year + 1, month=1)
    else:
        prev = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
        months = [prev, now]

    events: list[dict] = []
    for month_dt in months:
        key = _event_key(month_dt)
        try:
            resp = s3.get_object(Bucket=backup_bucket, Key=key)
            for line in resp["Body"].read().decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if source and event.get("source") != source:
                        continue
                    if since:
                        ts = datetime.fromisoformat(event["timestamp"])
                        if ts < since:
                            continue
                    events.append(event)
                except (json.JSONDecodeError, KeyError):
                    continue
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
                continue
            raise

    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return events[:limit]
