"""Reporting commands."""

from typing import Literal

import typer

app = typer.Typer()


@app.command()
def report(
    period: str = typer.Option("30d", help="Report period (e.g., 7d, 30d, 90d)"),
    output_format: Literal["text", "json", "html", "pdf"] = typer.Option(
        "text", "--format", help="Output format"
    ),
):
    """Generate backup activity and cost reports."""
    typer.echo(f"Generating report - coming soon (period: {period}, format: {output_format})")


@app.command()
def compliance(
    output_format: Literal["html", "pdf"] = typer.Option("pdf", "--format", help="Output format"),
):
    """Generate compliance report for audit purposes."""
    typer.echo(f"Compliance report - coming soon (format: {output_format})")
