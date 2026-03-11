"""Manual backup execution command."""

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.dynamodb_backup import ensure_dynamodb_backup_bucket_ready, export_dynamodb_table
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.s3_backup import backup_source, get_cross_account_session
from nzshm_backup.s3_batch import batch_backup_source
from nzshm_backup.state import get_state

app = typer.Typer()


@app.callback(invoke_without_command=True)
def run(
    source: str = typer.Option("all", help="Data source to backup, or 'all'"),
    full_sync: bool = typer.Option(
        False, "--full-sync", help="Force full copy instead of incremental"
    ),
):
    """Execute manual backup.

    Triggers backup for specified source(s). Respects the global --dry-run flag.
    """
    state = get_state()
    logger = setup_logging(json_format=False, verbose=state.verbose)

    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source == "all":
        sources_to_backup = list(config.sources.keys())
    else:
        if source not in config.sources:
            valid = ", ".join(sorted(config.sources.keys()))
            typer.echo(f"Error: unknown source '{source}'. Valid sources: {valid}", err=True)
            raise typer.Exit(1)
        sources_to_backup = [source]

    region = config.general.region
    session = boto3.Session()

    if state.dry_run:
        account_id = "123456789012"
    else:
        account_id = session.client("sts").get_caller_identity()["Account"]

    total_results = {
        "objects_copied": 0,
        "bytes_transferred": 0,
        "objects_skipped": 0,
        "errors": [],
    }

    for source_alias in sources_to_backup:
        if source_alias not in config.sources:
            logger.error(f"Unknown source alias: {source_alias}")
            total_results["errors"].append(f"Unknown source: {source_alias}")
            continue

        source_config = config.sources[source_alias]
        source_session = (
            get_cross_account_session(session, source_config.prod_account_role_arn)
            if source_config.prod_account_role_arn
            else None
        )

        for bucket_arn in source_config.s3_buckets:
            bucket_name = bucket_arn.split(":")[-1] if ":" in bucket_arn else bucket_arn
            backup_bucket_name = source_config.get_backup_bucket_name(
                bucket_arn, region, account_id
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
                        dry_run=state.dry_run,
                        full_sync=full_sync,
                        source_session=source_session,
                    )
                    prefix = "[DRY RUN] " if state.dry_run else ""
                    if batch_result.status == "SKIPPED":
                        typer.echo(f"{prefix}Batch: nothing to copy for {bucket_name}")
                    else:
                        typer.echo(
                            f"{prefix}Batch job submitted: {batch_result.job_id} "
                            f"({batch_result.objects_in_manifest} objects)"
                        )
                else:
                    result = backup_source(
                        session=session,
                        source_bucket=bucket_arn,
                        backup_bucket_name=backup_bucket_name,
                        dry_run=state.dry_run,
                        full_sync=full_sync,
                        source_session=source_session,
                    )

                    total_results["objects_copied"] += result.objects_copied
                    total_results["bytes_transferred"] += result.bytes_transferred
                    total_results["objects_skipped"] += result.objects_skipped

                    if state.dry_run:
                        logger.info(
                            f"[DRY RUN] Would copy {result.objects_copied} objects "
                            f"({result.bytes_transferred} bytes)"
                        )
                    else:
                        logger.info(
                            f"Copied {result.objects_copied} objects "
                            f"({result.bytes_transferred / (1024 * 1024):.2f} MB) "
                            f"in {result.duration_seconds:.1f}s"
                        )

            except Exception as e:
                logger.error(f"Backup failed for {bucket_name}: {e}")
                total_results["errors"].append(f"{bucket_name}: {str(e)}")

        dynamodb_client = (source_session or session).client("dynamodb")
        for table_arn in source_config.dynamodb_tables:
            export_bucket = source_config.get_dynamodb_backup_bucket_name(
                source_alias, region, account_id
            )
            if not state.dry_run:
                ensure_dynamodb_backup_bucket_ready(session, export_bucket)
            result = export_dynamodb_table(
                dynamodb_client,
                table_arn,
                export_bucket,
                source_config.dynamodb_export_format,
                state.dry_run,
            )
            if result.success:
                prefix = "[DRY RUN] " if state.dry_run else ""
                typer.echo(
                    f"{prefix}Export initiated: {result.table_name} → "
                    f"{result.export_arn or 'skipped'}"
                )
            else:
                total_results["errors"].extend(
                    [f"{e['table_arn']}: {e['error']}" for e in result.errors]
                )

    if total_results["errors"]:
        typer.echo(f"\nCompleted with {len(total_results['errors'])} error(s)", err=True)
        for error in total_results["errors"]:
            typer.echo(f"  - {error}", err=True)
        raise typer.Exit(1)
    else:
        typer.echo("\nBackup completed successfully")
        if state.dry_run:
            typer.echo(
                f"[DRY RUN] Would copy {total_results['objects_copied']} objects "
                f"({total_results['bytes_transferred'] / (1024 * 1024):.2f} MB)"
            )
