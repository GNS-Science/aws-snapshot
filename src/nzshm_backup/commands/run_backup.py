"""Manual backup execution command."""

from typing import TypedDict

import boto3
import typer

from nzshm_backup.backup_engine import run_backup_source
from nzshm_backup.config import load_config
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.state import get_state

app = typer.Typer()


@app.callback(invoke_without_command=True)
def run(
    source: str = typer.Option("all", help="Data source to backup, or 'all'"),
    full_sync: bool = typer.Option(
        False, "--full-sync", help="Force full copy instead of incremental"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
    prepare_only: bool = typer.Option(
        False,
        "--prepare-only",
        help="Build S3 Batch manifest(s) but do not submit S3 Batch jobs",
    ),
):
    """Execute manual backup.

    Triggers backup for specified source(s). Respects the global --dry-run flag.
    """
    state = get_state()
    if dry_run:
        state.dry_run = True
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

    session = boto3.Session()

    class _Totals(TypedDict):
        objects_copied: int
        bytes_transferred: int
        objects_skipped: int
        errors: list[str]

    total_results: _Totals = {
        "objects_copied": 0,
        "bytes_transferred": 0,
        "objects_skipped": 0,
        "errors": [],
    }

    for source_alias in sources_to_backup:
        result = run_backup_source(
            session,
            config,
            source_alias,
            dry_run=state.dry_run,
            full_sync=full_sync,
            prepare_only=prepare_only,
        )

        # Accumulate S3 totals and emit per-bucket output
        for r in result.s3_results:
            if r["status"] == "error":
                logger.error(f"Backup failed for {r['bucket_name']}: {r['error']}")
            elif "batch_job_id" in r:
                prefix = "[DRY RUN] " if state.dry_run else ""
                if state.dry_run:
                    typer.echo(
                        f"{prefix}Would submit S3 Batch job for {r['bucket_name']} "
                        f"(object count not enumerated — use 'backup check' for access validation)"
                    )
                elif r["batch_status"] == "SKIPPED":
                    typer.echo(f"{prefix}Batch: nothing to copy for {r['bucket_name']}")
                elif r["batch_status"] == "PREPARED":
                    typer.echo(
                        f"{prefix}Manifest prepared for {r['bucket_name']} "
                        f"({r['objects_in_manifest']} objects): {r['manifest_key']}"
                    )
                else:
                    typer.echo(
                        f"{prefix}Batch job submitted: {r['batch_job_id']} "
                        f"({r['objects_in_manifest']} objects)"
                    )
            else:
                total_results["objects_copied"] += r.get("objects_copied", 0)
                total_results["bytes_transferred"] += r.get("bytes_transferred", 0)
                total_results["objects_skipped"] += r.get("objects_skipped", 0)

                if state.dry_run:
                    logger.info(
                        f"[DRY RUN] Would copy {r['objects_copied']} objects "
                        f"({r['bytes_transferred']} bytes)"
                    )
                else:
                    logger.info(
                        f"Copied {r['objects_copied']} objects "
                        f"({r['bytes_transferred'] / (1024 * 1024):.2f} MB) "
                        f"in {r['duration_seconds']:.1f}s"
                    )

        # Emit per-table output
        for r in result.dynamodb_results:
            if r["status"] == "success":
                prefix = "[DRY RUN] " if state.dry_run else ""
                typer.echo(
                    f"{prefix}Export initiated: {r['table_name']} → {r['export_arn'] or 'skipped'}"
                )

        total_results["errors"].extend(result.errors)

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
