"""Manual backup execution command."""

import typer
from typing import Optional, Literal

app = typer.Typer()


@app.command()
def run(
    source: Literal["toshi", "ths", "all"] = typer.Option(
        "all", help="Data source to backup"
    ),
    dry_run: bool = typer.Option(
        False, help="Show what would be done without executing"
    ),
):
    """Execute manual backup.

    Triggers backup for specified source(s). Use --dry-run to preview actions.
    """
    if dry_run:
        typer.echo(f"[DRY RUN] Would trigger backup for: {source}")
    else:
        typer.echo(f"Starting backup for: {source} - implementation coming soon")
