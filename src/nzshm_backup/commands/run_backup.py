"""Manual backup execution command."""

from typing import Literal

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.s3_backup import backup_source
from nzshm_backup.state import get_state

app = typer.Typer()


@app.callback(invoke_without_command=True)
def run(
    source: Literal["toshi", "ths", "all"] = typer.Option("all", help="Data source to backup"),
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

        for bucket_arn in source_config.s3_buckets:
            bucket_name = bucket_arn.split(":")[-1] if ":" in bucket_arn else bucket_arn
            backup_bucket_name = source_config.get_backup_bucket_name(
                bucket_arn, region, account_id
            )

            logger.info(f"Backing up {bucket_name} → {backup_bucket_name}")

            try:
                result = backup_source(
                    session=session,
                    source_bucket=bucket_arn,
                    backup_bucket_name=backup_bucket_name,
                    dry_run=state.dry_run,
                    full_sync=full_sync,
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
