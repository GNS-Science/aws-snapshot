"""Cost management commands."""

import typer
from typing import Optional, Literal

app = typer.Typer()


@app.command()
def export(
    format: Literal["csv", "json"] = typer.Option("csv", help="Export format"),
    output_to: Optional[str] = typer.Option(
        None, help="S3 path or local directory for export"
    ),
):
    """Export cost data for finance systems."""
    typer.echo(f"Cost export - coming soon (format: {format})")
