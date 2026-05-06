"""Inventory configuration and freshness helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from nzshm_backup.s3_backup import get_account_id


def _expected_prefix(source_alias: str, side: str, bucket: str, root: str = "inventory") -> str:
    return f"{root}/{source_alias}/{side}/{bucket}"


def _dest_bucket_name(dest_bucket_arn: str) -> str:
    return dest_bucket_arn.split(":::")[-1]


def _inventory_config_for_prefix(
    s3_client, bucket: str, control_bucket: str, expected_prefix: str
) -> dict[str, Any] | None:
    try:
        response = s3_client.list_bucket_inventory_configurations(Bucket=bucket)
    except ClientError:
        return None
    except Exception:
        return None

    configs = response.get("InventoryConfigurationList", []) if isinstance(response, dict) else []
    if not isinstance(configs, list):
        return None

    normalized_prefix = expected_prefix.rstrip("/")
    for cfg in configs:
        try:
            dest = cfg["Destination"]["S3BucketDestination"]
            if _dest_bucket_name(dest["Bucket"]) != control_bucket:
                continue
            if str(dest.get("Prefix", "")).rstrip("/") != normalized_prefix:
                continue
            return dict(cfg)
        except Exception:
            continue
    return None


def _latest_object_ts(s3_client, bucket: str, prefix: str) -> datetime | None:
    paginator = s3_client.get_paginator("list_objects_v2")
    latest: datetime | None = None
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix.rstrip('/')}/"):
            for obj in page.get("Contents", []) or []:
                ts = obj.get("LastModified")
                if isinstance(ts, datetime) and (latest is None or ts > latest):
                    latest = ts
    except ClientError:
        return None
    except Exception:
        return None
    return latest


def inventory_health_for_bucket_pair(
    backup_session: boto3.Session,
    source_session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
) -> dict[str, Any]:
    """Return inventory config/freshness signals for a source/backup bucket pair."""
    backup_s3 = backup_session.client("s3")
    source_s3 = source_session.client("s3")

    backup_account_id = get_account_id(backup_session)
    control_bucket = f"nzshm-backup-inventory-{backup_account_id}"

    source_prefix = _expected_prefix(source_alias, "source", source_bucket)
    backup_prefix = _expected_prefix(source_alias, "backup", backup_bucket)

    source_cfg = _inventory_config_for_prefix(
        source_s3, source_bucket, control_bucket, source_prefix
    )
    backup_cfg = _inventory_config_for_prefix(
        backup_s3, backup_bucket, control_bucket, backup_prefix
    )

    source_latest = _latest_object_ts(backup_s3, control_bucket, source_prefix)
    backup_latest = _latest_object_ts(backup_s3, control_bucket, backup_prefix)

    effective = None
    if source_latest and backup_latest:
        effective = min(source_latest, backup_latest)

    return {
        "control_bucket": control_bucket,
        "source_prefix": source_prefix,
        "backup_prefix": backup_prefix,
        "source_configured": bool(source_cfg and source_cfg.get("IsEnabled", False)),
        "backup_configured": bool(backup_cfg and backup_cfg.get("IsEnabled", False)),
        "source_latest": source_latest,
        "backup_latest": backup_latest,
        "effective_data_ts": effective,
    }
