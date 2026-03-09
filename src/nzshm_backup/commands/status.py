"""Status command - current backup state."""

import typer
from typing import Optional, Literal

app = typer.Typer()


@app.command()
def status(
    source: Literal["toshi", "ths", "all"] = typer.Option(
        "all", help="Data source to check"
    ),
    output: Literal["text", "json", "yaml"] = typer.Option(
        "text", help="Output format"
    ),
):
    """Show current backup status.

    Displays last backup time, next scheduled run, and overall health.
    """
    typer.echo(f"Backup status - coming soon (source: {source}, format: {output})")
