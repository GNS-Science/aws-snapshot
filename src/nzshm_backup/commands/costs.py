"""Cost management commands."""

from typing import Literal, Optional

import typer

app = typer.Typer()


@app.command()
def predict(
    current: float = typer.Option(20400.0, help="Current annual cost (NZD)"),
    target: float = typer.Option(7420.0, help="Target annual cost (NZD)"),
):
    """Project before/after cost savings."""
    typer.echo(f"Cost prediction - coming soon (current: ${current}, target: ${target})")


@app.command()
def report(
    period: str = typer.Option("last-month", help="Reporting period (e.g. last-month, last-week)"),
):
    """Show cost report for a given period."""
    typer.echo(f"Cost report - coming soon (period: {period})")


@app.command()
def breakdown(
    by: Literal["source", "tier", "service"] = typer.Option(
        "source", help="Dimension to break costs down by"
    ),
):
    """Show cost breakdown by source, storage tier, or AWS service."""
    typer.echo(f"Cost breakdown - coming soon (by: {by})")


@app.command()
def export(
    output_format: Literal["csv", "json"] = typer.Option("csv", "--format", help="Export format"),
    output_to: Optional[str] = typer.Option(
        None, help="S3 path or local directory for export"
    ),
):
    """Export cost data for finance systems."""
    typer.echo(f"Cost export - coming soon (format: {output_format})")
