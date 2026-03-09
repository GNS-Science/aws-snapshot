"""Restore operations commands."""

from typing import Literal

import typer

app = typer.Typer()


@app.command("list")
def list_restores(
    source: Literal["toshi", "ths"] | None = typer.Option(None, help="Filter by source"),
    limit: int = typer.Option(10, help="Number of restore points to show"),
):
    """List available restore points."""
    typer.echo(f"Listing restore points - coming soon (source: {source}, limit: {limit})")


@app.command("preview")
def preview(
    date: str = typer.Option(..., help="Backup date to restore (YYYY-MM-DD)"),
    source: Literal["toshi", "ths"] | None = typer.Option(None, help="Data source"),
    target_bucket: str | None = typer.Option(None, help="Destination bucket for restore"),
):
    """Preview restore operation with cost estimate."""
    typer.echo(f"Restore preview - coming soon: {date} for {source}")


@app.command("run")
def run_restore(
    date: str = typer.Option(..., help="Backup date to restore (YYYY-MM-DD)"),
    source: Literal["toshi", "ths"] | None = typer.Option(None, help="Data source"),
    target_bucket: str | None = typer.Option(None, help="Destination bucket for restore"),
    table: str | None = typer.Option(None, help="DynamoDB table to restore"),
    prefix: str | None = typer.Option(None, help="S3 prefix to restore (subset)"),
):
    """Execute restore operation."""
    typer.echo(f"Restore execution - coming soon: {date}")


@app.command("cancel")
def cancel(
    job_id: str = typer.Option(..., help="Restore job ID to cancel"),
):
    """Cancel in-progress restore."""
    typer.echo(f"Cancelling restore job {job_id} - coming soon")
