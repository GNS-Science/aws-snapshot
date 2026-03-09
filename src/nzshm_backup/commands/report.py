"""Reporting commands."""

import typer
from typing import Optional, Literal

app = typer.Typer()


@app.command()
def report(
    period: str = typer.Option("30d", help="Report period (e.g., 7d, 30d, 90d)"),
    format: Literal["text", "json", "html", "pdf"] = typer.Option(
        "text", help="Output format"
    ),
):
    """Generate backup activity and cost reports."""
    typer.echo(f"Generating report - coming soon (period: {period}, format: {format})")
