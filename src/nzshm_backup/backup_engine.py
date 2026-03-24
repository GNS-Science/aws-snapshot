"""Core per-source backup logic, shared between CLI and Lambda handler."""

import logging
from dataclasses import dataclass, field

import boto3

from nzshm_backup.config.models import ConfigModel
from nzshm_backup.dynamodb_backup import ensure_dynamodb_backup_bucket_ready, export_dynamodb_table
from nzshm_backup.event_log import append_event
from nzshm_backup.run_state import write_run_state
from nzshm_backup.s3_backup import backup_source, get_cross_account_session
from nzshm_backup.s3_batch import batch_backup_source

logger = logging.getLogger(__name__)


@dataclass
class SourceBackupResult:
    """Aggregated result of backing up a single source (S3 + DynamoDB)."""

    source_alias: str
    s3_results: list[dict] = field(default_factory=list)       # one entry per bucket
    dynamodb_results: list[dict] = field(default_factory=list)  # one entry per table
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def run_backup_source(
    session: boto3.Session,
    config: ConfigModel,
    source_alias: str,
    dry_run: bool = False,
    full_sync: bool = False,
) -> SourceBackupResult:
    """Execute backup for a single named source.

    Handles both S3 (direct copy or Batch Operations) and DynamoDB
    (Point-in-Time export) for every bucket/table listed under *source_alias*
    in *config*.

    Args:
        session:       boto3 Session for the backup (destination) account.
        config:        Loaded ConfigModel.
        source_alias:  Key in ``config.sources`` to back up.
        dry_run:       Simulate without writing to AWS.
        full_sync:     Force full copy instead of incremental sync.

    Returns:
        SourceBackupResult with per-bucket/table dicts and any error strings.

    Raises:
        KeyError: If *source_alias* is not present in ``config.sources``.
    """
    source_config = config.sources[source_alias]  # raises KeyError on unknown alias
    region = config.general.region

    caller = session.client("sts").get_caller_identity()
    account_id = caller["Account"]
    actor = caller.get("Arn")

    source_session = (
        get_cross_account_session(session, source_config.source_account_role_arn)
        if source_config.source_account_role_arn
        else None
    )
    source_account_id = source_config.source_account_id or account_id

    result = SourceBackupResult(source_alias=source_alias)

    # ------------------------------------------------------------------
    # S3 loop
    # ------------------------------------------------------------------
    for bucket_cfg in source_config.s3_buckets:
        bucket_name = bucket_cfg.arn.split(":")[-1] if ":" in bucket_cfg.arn else bucket_cfg.arn
        backup_bucket_name = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source_alias
        )

        logger.info(f"Backing up {bucket_name} → {backup_bucket_name}")

        try:
            if source_config.use_s3_batch:
                batch_result = batch_backup_source(
                    session=session,
                    source_bucket=bucket_name,
                    backup_bucket=backup_bucket_name,
                    batch_role_arn=config.general.s3_batch_role_arn,
                    account_id=account_id,
                    dry_run=dry_run,
                    full_sync=full_sync,
                    source_session=source_session,
                )
                result.s3_results.append(
                    {
                        "bucket_name": bucket_name,
                        "status": "success",
                        "batch_job_id": batch_result.job_id,
                        "batch_status": batch_result.status,
                        "objects_in_manifest": batch_result.objects_in_manifest,
                        "manifest_key": batch_result.manifest_key,
                        "dry_run": batch_result.dry_run,
                    }
                )
                if not dry_run:
                    write_run_state(
                        session, backup_bucket_name, bucket_name,
                        status=batch_result.status.lower(),
                        batch_job_id=batch_result.job_id,
                        objects_in_manifest=batch_result.objects_in_manifest,
                    )
                    append_event(
                        session, backup_bucket_name, "backup_run", source_alias,
                        details={
                            "bucket": bucket_name,
                            "mode": "batch",
                            "status": batch_result.status.lower(),
                            "batch_job_id": batch_result.job_id,
                            "objects_in_manifest": batch_result.objects_in_manifest,
                        },
                        actor=actor,
                    )
            else:
                sync_result = backup_source(
                    session=session,
                    source_bucket=bucket_cfg.arn,
                    backup_bucket_name=backup_bucket_name,
                    dry_run=dry_run,
                    full_sync=full_sync,
                    source_session=source_session,
                )
                result.s3_results.append(
                    {
                        "bucket_name": bucket_name,
                        "status": "success",
                        "objects_copied": sync_result.objects_copied,
                        "bytes_transferred": sync_result.bytes_transferred,
                        "objects_skipped": sync_result.objects_skipped,
                        "duration_seconds": sync_result.duration_seconds,
                        "dry_run": sync_result.dry_run,
                    }
                )
                if not dry_run:
                    status = "completed" if sync_result.objects_copied > 0 else "skipped"
                    write_run_state(
                        session, backup_bucket_name, bucket_name,
                        status=status,
                        objects_copied=sync_result.objects_copied,
                        bytes_transferred=sync_result.bytes_transferred,
                    )
                    append_event(
                        session, backup_bucket_name, "backup_run", source_alias,
                        details={
                            "bucket": bucket_name,
                            "mode": "incremental",
                            "status": status,
                            "objects_copied": sync_result.objects_copied,
                            "bytes_transferred": sync_result.bytes_transferred,
                        },
                        actor=actor,
                    )

        except Exception as e:
            logger.error(f"Backup failed for {bucket_name}: {e}")
            result.s3_results.append(
                {
                    "bucket_name": bucket_name,
                    "status": "error",
                    "error": str(e),
                }
            )
            result.errors.append(f"{bucket_name}: {str(e)}")

    # ------------------------------------------------------------------
    # DynamoDB loop
    # ------------------------------------------------------------------
    dynamodb_client = (source_session or session).client("dynamodb")
    for table_arn in source_config.dynamodb_tables:
        table_name = table_arn.split("/")[-1]
        export_bucket = source_config.get_dynamodb_backup_bucket_name(
            source_alias, region, source_account_id
        )

        if not dry_run:
            ensure_dynamodb_backup_bucket_ready(
                session, export_bucket, source_alias=source_alias,
                source_account_id=source_account_id,
            )

        export_result = export_dynamodb_table(
            dynamodb_client,
            table_arn,
            export_bucket,
            source_config.dynamodb_export_format,
            dry_run,
            s3_bucket_owner=account_id,
        )

        result.dynamodb_results.append(
            {
                "table_name": table_name,
                "status": "success" if export_result.success else "error",
                "export_arn": export_result.export_arn,
                "export_bucket": export_result.export_bucket,
                "export_prefix": export_result.export_prefix,
                "dry_run": export_result.dry_run,
                "errors": export_result.errors,
            }
        )

        if not export_result.success:
            result.errors.extend(
                [f"{e['table_arn']}: {e['error']}" for e in export_result.errors]
            )

    return result
