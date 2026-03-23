"""Restore operations commands."""

from datetime import datetime, timezone

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.dynamodb_restore import (
    PITR_WATCHER_RULE_NAME,
    describe_restore_status,
    make_restore_table_name,
    restore_dynamodb_table,
)
from nzshm_backup.restore_state import add_pending_restore
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.s3_batch import batch_restore_bucket, list_recent_batch_jobs
from nzshm_backup.s3_restore import (
    _ensure_restore_target,
    apply_restore_target_policy,
    make_restore_bucket_name,
    restore_s3_bucket,
)
from nzshm_backup.state import get_state

app = typer.Typer()

RESTORE_STATUS_ICON = {
    "ACTIVE": "✓",
    "RESTORING": "⋯",
    "CREATING": "⋯",
    "FAILED": "✗",
}


def _parse_point_in_time(ts: str) -> datetime:
    """Parse ISO timestamp, attaching UTC if no timezone given."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@app.command("run")
def run_restore(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    buckets: list[str] = typer.Option(
        [], "--buckets", help="Bucket labels to restore (default: all configured)"
    ),
    tables: list[str] = typer.Option(
        [], "--tables", help="Table names to restore (default: all configured)"
    ),
    original: bool = typer.Option(
        False, "--original",
        help="Restore directly into the original source bucket. "
             "Use only if the original bucket no longer exists. "
             "Normal DR should use the default -restore target to allow parallel forensics.",
    ),
    target_table: str | None = typer.Option(
        None, help="DynamoDB target table name (single table only)"
    ),
    to_point_in_time: str | None = typer.Option(
        None, "--to-point-in-time",
        help="ISO datetime for DynamoDB PITR, e.g. 2026-03-15T09:00:00Z (required when restoring tables)",
    ),
    prefix: str | None = typer.Option(None, help="Restore only objects under this S3 key prefix"),
    no_pitr: bool = typer.Option(
        False, "--no-pitr",
        help="Skip automatic PITR re-enable after DynamoDB restore. "
             "Use only for short-lived test restores that will be deleted immediately.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
):
    """Execute a restore from backup.

    Restores S3 buckets and/or DynamoDB tables for the given source.
    Use --buckets / --tables to select a subset; omit both to restore everything
    configured under --source.

    S3 restore (default): writes to {source-bucket}-restore (truncated to 63 chars).
    This preserves the original bucket for forensics and allows parallel recovery verification.
    Pass --original only if the original bucket no longer exists.
    Target buckets must already exist; S3 bucket names are permanent and cannot be renamed.

    For cross-account restores the AllowNzshmBatchRoleWrite bucket policy is applied to
    the target bucket at runtime (before the Batch job is submitted).

    DynamoDB restores are submit-and-return (async). Use 'restore status'
    to check progress.
    """
    state = get_state()
    if dry_run:
        state.dry_run = True

    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    source_config = config.sources[source]
    region = config.general.region
    session = boto3.Session()
    account_id = get_account_id(session)
    source_account_id = source_config.source_account_id or account_id

    # Resolve which buckets / tables to act on.
    # If neither --buckets nor --tables is given, restore everything.
    # If either is given, it selects only that type — specifying --buckets
    # does not implicitly also restore all tables, and vice versa.
    if buckets or tables:
        effective_buckets = [b for b in source_config.s3_buckets if b.label in buckets] if buckets else []
        effective_table_arns = (
            [arn for arn in source_config.dynamodb_tables if arn.split("/")[-1] in tables]
            if tables else []
        )
    else:
        effective_buckets = list(source_config.s3_buckets)
        effective_table_arns = list(source_config.dynamodb_tables)

    if not effective_buckets and not effective_table_arns:
        typer.echo("Nothing to restore — no matching buckets or tables configured.", err=True)
        raise typer.Exit(1)

    # Validate target overrides
    if target_table and len(effective_table_arns) > 1:
        typer.echo(
            "Error: --target-table is only valid for a single table restore. "
            "Select one with --tables.", err=True
        )
        raise typer.Exit(1)

    if effective_table_arns and not to_point_in_time and not state.dry_run:
        typer.echo(
            "Error: --to-point-in-time is required when restoring DynamoDB tables.", err=True
        )
        raise typer.Exit(1)

    restore_point = _parse_point_in_time(to_point_in_time) if to_point_in_time else None
    errors: list[str] = []

    # ------------------------------------------------------------------
    # S3 restore
    # ------------------------------------------------------------------
    batch_role_arn = config.general.s3_batch_role_arn
    is_cross_account = account_id != source_account_id

    restore_role_arn_s3 = (
        source_config.source_account_restore_role_arn
        or source_config.source_account_role_arn
    )
    source_session_s3 = (
        get_cross_account_session(session, restore_role_arn_s3)
        if restore_role_arn_s3 and is_cross_account
        else session
    )

    for bucket_cfg in effective_buckets:
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )
        source_bucket_name = bucket_cfg.arn.split(":::")[-1]
        dest_bucket = source_bucket_name if original else make_restore_bucket_name(source_bucket_name)
        prefix_info = f" (prefix: {prefix})" if prefix else ""
        typer.echo(f"  Restoring S3: {backup_bucket} → {dest_bucket}{prefix_info}")

        if state.dry_run:
            typer.echo(f"  [DRY RUN] Would restore {backup_bucket} → {dest_bucket}")
            continue

        source_s3_client = source_session_s3.client("s3")

        if not original:
            _ensure_restore_target(source_s3_client, dest_bucket, region)

        if batch_role_arn and is_cross_account:
            try:
                apply_restore_target_policy(source_s3_client, dest_bucket, batch_role_arn)
            except Exception as e:
                typer.echo(f"  Warning: could not apply write policy to {dest_bucket}: {e}", err=True)

        if batch_role_arn:
            result = batch_restore_bucket(
                session, backup_bucket, dest_bucket, batch_role_arn, account_id, prefix=prefix
            )
            if result.status == "SUBMITTED":
                typer.echo(
                    f"  ✓ Batch job submitted: {result.job_id} "
                    f"({result.objects_in_manifest} objects)"
                )
                typer.echo(f"    Check progress: backup restore status --source {source}")
            elif result.status == "SKIPPED":
                typer.echo("  ✓ Nothing to restore — backup bucket is empty")
            else:
                for err in result.errors:
                    typer.echo(f"  ✗ {err['error']}", err=True)
                errors.append(f"{dest_bucket}: batch job failed")
        else:
            typer.echo(
                "    (s3_batch_role_arn not configured — using direct copy; "
                "set general.s3_batch_role_arn for large-bucket restores)"
            )
            direct_result = restore_s3_bucket(session, backup_bucket, dest_bucket, prefix=prefix)
            if direct_result.success:
                mb = direct_result.bytes_transferred / (1024 * 1024)
                typer.echo(
                    f"  ✓ {direct_result.objects_copied} objects copied ({mb:.1f} MB), "
                    f"{direct_result.objects_skipped} skipped"
                )
            else:
                typer.echo(
                    f"  ✗ Restore completed with {len(direct_result.errors)} error(s)", err=True
                )
                for err in direct_result.errors:
                    typer.echo(f"    - {err['key']}: {err['error']}", err=True)
                errors.append(f"{dest_bucket}: {len(direct_result.errors)} copy errors")

    # ------------------------------------------------------------------
    # DynamoDB restore
    # ------------------------------------------------------------------
    if effective_table_arns:
        restore_role_arn = (
            source_config.source_account_restore_role_arn
            or source_config.source_account_role_arn
        )
        source_session = (
            get_cross_account_session(session, restore_role_arn)
            if restore_role_arn and account_id != source_account_id
            else session
        )
        dynamodb_client = source_session.client("dynamodb")
        ssm_client = session.client("ssm")

        for table_arn in effective_table_arns:
            table_name = table_arn.split("/")[-1]
            dest_table = target_table if target_table else make_restore_table_name(table_arn)
            if state.dry_run:
                typer.echo(f"  [DRY RUN] Would submit PITR restore: {table_name} → {dest_table}")
                continue

            typer.echo(
                f"  Restoring DynamoDB: {table_name} → {dest_table} "
                f"at {restore_point.isoformat()}"
            )

            result = restore_dynamodb_table(
                dynamodb_client, table_arn, dest_table, restore_point,
            )

            if result.success:
                typer.echo(f"  ✓ Restore submitted: {dest_table} ({result.restore_arn})")
                if not no_pitr and result.restore_arn:
                    add_pending_restore(
                        ssm_client,
                        restore_arn=result.restore_arn,
                        source=source,
                        source_table_arn=table_arn,
                        restore_point_iso=restore_point.isoformat(),
                    )
                    typer.echo("    PITR will be re-enabled automatically once ACTIVE")
                typer.echo(
                    f"    Check progress: backup restore status "
                    f"--source {source} --tables {table_name}"
                )
            else:
                for err in result.errors:
                    typer.echo(f"  ✗ {err['error']}", err=True)
                errors.append(f"{dest_table}: restore failed")

    # Activate the pitr-watcher rule if any DynamoDB restores were submitted with PITR enabled
    dynamo_submitted = effective_table_arns and not state.dry_run and not no_pitr
    if dynamo_submitted and not errors:
        try:
            session.client("events").enable_rule(Name=PITR_WATCHER_RULE_NAME)
            typer.echo(f"  pitr-watcher rule enabled ({PITR_WATCHER_RULE_NAME})")
        except Exception as e:
            typer.echo(f"  Warning: could not enable pitr-watcher rule: {e}", err=True)

    typer.echo("")
    if errors:
        for e in errors:
            typer.echo(f"  ERROR: {e}", err=True)
        raise typer.Exit(1)


RESTORE_BATCH_STATUS_ICON = {
    "Complete": "✓",
    "Failed": "✗",
    "Cancelled": "✗",
    "Active": "⋯",
    "Completing": "⋯",
    "Preparing": "⋯",
    "New": "⋯",
    "Paused": "⏸",
    "Suspended": "⏸",
}
RESTORE_BATCH_JOB_LIMIT = 3


@app.command("status")
def restore_status(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    buckets: list[str] = typer.Option(
        [], "--buckets", help="Bucket labels to check (default: all configured)"
    ),
    tables: list[str] = typer.Option(
        [], "--tables", help="Table names to check (default: all configured)"
    ),
):
    """Show status of in-progress restores (S3 Batch jobs and DynamoDB PITR restores).

    S3: shows recent restore batch jobs for each configured bucket.
    DynamoDB: queries the live restored-table status. Restored table names follow
    the <original>-restore convention unless overridden at restore time.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    source_config = config.sources[source]
    region = config.general.region
    session = boto3.Session()
    current_account = get_account_id(session)
    source_account_id = source_config.source_account_id or current_account

    typer.echo(f"\n[{source}] restore status:")

    # ------------------------------------------------------------------
    # S3 Batch restore jobs
    # ------------------------------------------------------------------
    effective_buckets = [
        b for b in source_config.s3_buckets
        if not buckets or b.label in buckets
    ]
    if effective_buckets:
        typer.echo("\n  S3 restore jobs:")
        s3control = session.client("s3control", region_name=region)
        for bucket_cfg in effective_buckets:
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_cfg.label, region, source_account_id, source
            )
            try:
                jobs = list_recent_batch_jobs(
                    s3control, current_account, backup_bucket,
                    limit=RESTORE_BATCH_JOB_LIMIT,
                )
                # Filter to restore jobs only (description starts with "nzshm-restore:")
                jobs = [j for j in jobs if "nzshm-restore:" in j.get("Description", "")]
                if not jobs:
                    typer.echo(f"    {bucket_cfg.label}: no restore jobs found")
                    continue
                for job in jobs:
                    status = job.get("Status", "Unknown")
                    icon = RESTORE_BATCH_STATUS_ICON.get(status, "?")
                    job_id = job.get("JobId", "")[:8]
                    created = job.get("CreationTime")
                    ts = f"  [{created.strftime('%Y-%m-%d %H:%M UTC')}]" if created else ""
                    progress = job.get("ProgressSummary", {})
                    total = progress.get("TotalNumberOfTasks", 0)
                    failed = progress.get("NumberOfTasksFailed", 0)
                    desc = job.get("Description", "")
                    target = desc.split("→")[-1].strip() if "→" in desc else ""
                    if status == "Complete" and failed == 0:
                        progress_str = f"{total}/{total} objects"
                    elif failed:
                        progress_str = f"{failed} failed / {total} objects"
                    else:
                        succeeded = progress.get("NumberOfTasksSucceeded", 0)
                        progress_str = f"{succeeded}/{total} objects"
                    typer.echo(
                        f"    {icon} {bucket_cfg.label} → {target}: {status}  "
                        f"job/{job_id}…{ts}  ({progress_str})"
                    )
            except Exception as e:
                typer.echo(f"    {bucket_cfg.label}: error fetching restore status ({e})")

    # ------------------------------------------------------------------
    # DynamoDB PITR restore status
    # ------------------------------------------------------------------
    effective_table_arns = [
        arn for arn in source_config.dynamodb_tables
        if not tables or arn.split("/")[-1] in tables
    ]
    if effective_table_arns:
        restore_role_arn = (
            source_config.source_account_restore_role_arn
            or source_config.source_account_role_arn
        )
        source_session = (
            get_cross_account_session(session, restore_role_arn)
            if restore_role_arn and current_account != source_account_id
            else session
        )
        dynamodb_client = source_session.client("dynamodb")

        typer.echo("\n  DynamoDB restore status:")
        for table_arn in effective_table_arns:
            table_name = table_arn.split("/")[-1]
            restore_target = make_restore_table_name(table_arn)
            try:
                status = describe_restore_status(dynamodb_client, restore_target)
                icon = RESTORE_STATUS_ICON.get(status.table_status, "?")
                ts = (
                    f"  [{status.restore_date_time.strftime('%Y-%m-%d %H:%M UTC')}]"
                    if status.restore_date_time
                    else ""
                )
                typer.echo(f"    {icon} {table_name} → {restore_target}: {status.table_status}{ts}")
                if status.restore_in_progress:
                    typer.echo("      restore in progress...")
            except Exception as e:
                typer.echo(f"    ? {table_name} → {restore_target}: {e}")

    typer.echo("")
