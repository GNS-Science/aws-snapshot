"""Restore operations commands."""

from datetime import datetime, timezone

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.dynamodb_restore import (
    describe_restore_status,
    make_restore_table_name,
    restore_dynamodb_table,
)
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.s3_restore import restore_s3_bucket
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
    target_bucket: str | None = typer.Option(
        None, help="S3 destination bucket (single bucket only)"
    ),
    target_table: str | None = typer.Option(
        None, help="DynamoDB target table name (single table only)"
    ),
    to_point_in_time: str | None = typer.Option(
        None, "--to-point-in-time",
        help="ISO datetime for DynamoDB PITR, e.g. 2026-03-15T09:00:00Z (required when restoring tables)",
    ),
    prefix: str | None = typer.Option(None, help="Restore only objects under this S3 key prefix"),
):
    """Execute a restore from backup.

    Restores S3 buckets and/or DynamoDB tables for the given source.
    Use --buckets / --tables to select a subset; omit both to restore everything
    configured under --source.

    DynamoDB restores are submit-and-return (async). Use 'restore status'
    to check progress.
    """
    state = get_state()

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
    if target_bucket and len(effective_buckets) > 1:
        typer.echo(
            "Error: --target-bucket is only valid for a single bucket restore. "
            "Select one with --buckets.", err=True
        )
        raise typer.Exit(1)

    if target_table and len(effective_table_arns) > 1:
        typer.echo(
            "Error: --target-table is only valid for a single table restore. "
            "Select one with --tables.", err=True
        )
        raise typer.Exit(1)

    if effective_table_arns and not to_point_in_time:
        typer.echo(
            "Error: --to-point-in-time is required when restoring DynamoDB tables.", err=True
        )
        raise typer.Exit(1)

    restore_point = _parse_point_in_time(to_point_in_time) if to_point_in_time else None
    errors: list[str] = []

    # ------------------------------------------------------------------
    # S3 restore
    # ------------------------------------------------------------------
    for bucket_cfg in effective_buckets:
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )
        dest_bucket = target_bucket or bucket_cfg.arn.split(":::")[-1]
        prefix_info = f" (prefix: {prefix})" if prefix else ""
        typer.echo(f"  Restoring S3: {backup_bucket} → {dest_bucket}{prefix_info}")

        if state.dry_run:
            typer.echo(f"  [DRY RUN] Would restore {backup_bucket} → {dest_bucket}")
            continue

        result = restore_s3_bucket(session, backup_bucket, dest_bucket, prefix=prefix)

        if result.success:
            mb = result.bytes_transferred / (1024 * 1024)
            typer.echo(
                f"  ✓ {result.objects_copied} objects copied ({mb:.1f} MB), "
                f"{result.objects_skipped} skipped"
            )
        else:
            typer.echo(f"  ✗ Restore completed with {len(result.errors)} error(s)", err=True)
            for err in result.errors:
                typer.echo(f"    - {err['key']}: {err['error']}", err=True)
            errors.append(f"{dest_bucket}: {len(result.errors)} copy errors")

    # ------------------------------------------------------------------
    # DynamoDB restore
    # ------------------------------------------------------------------
    if effective_table_arns:
        source_session = (
            get_cross_account_session(session, source_config.source_account_role_arn)
            if source_config.source_account_role_arn
            else session
        )
        dynamodb_client = source_session.client("dynamodb")

        for table_arn in effective_table_arns:
            table_name = table_arn.split("/")[-1]
            dest_table = target_table if target_table else make_restore_table_name(table_arn)
            typer.echo(
                f"  Restoring DynamoDB: {table_name} → {dest_table} "
                f"at {restore_point.isoformat()}"
            )

            if state.dry_run:
                typer.echo(f"  [DRY RUN] Would submit PITR restore for {table_name}")
                continue

            result = restore_dynamodb_table(
                dynamodb_client, table_arn, dest_table, restore_point
            )

            if result.success:
                typer.echo(f"  ✓ Restore submitted: {dest_table} ({result.restore_arn})")
                typer.echo(
                    f"    Check progress: backup restore status "
                    f"--source {source} --tables {table_name}"
                )
            else:
                for err in result.errors:
                    typer.echo(f"  ✗ {err['error']}", err=True)
                errors.append(f"{dest_table}: restore failed")

    typer.echo("")
    if errors:
        for e in errors:
            typer.echo(f"  ERROR: {e}", err=True)
        raise typer.Exit(1)


@app.command("status")
def restore_status(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    tables: list[str] = typer.Option(
        [], "--tables", help="Table names to check (default: all configured)"
    ),
):
    """Show status of in-progress DynamoDB restores.

    Queries the live table status. Restored table names follow the
    <original>-restored convention unless overridden at restore time.
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
    effective_table_arns = [
        arn for arn in source_config.dynamodb_tables
        if not tables or arn.split("/")[-1] in tables
    ]

    if not effective_table_arns:
        typer.echo("No DynamoDB tables configured for this source.")
        return

    session = boto3.Session()
    source_session = (
        get_cross_account_session(session, source_config.source_account_role_arn)
        if source_config.source_account_role_arn
        else session
    )
    dynamodb_client = source_session.client("dynamodb")

    typer.echo(f"\n[{source}] DynamoDB restore status:\n")
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
            typer.echo(f"  {icon} {table_name} → {restore_target}: {status.table_status}{ts}")
            if status.restore_in_progress:
                typer.echo("    restore in progress...")
        except Exception as e:
            typer.echo(f"  ? {table_name} → {restore_target}: {e}")

    typer.echo("")
