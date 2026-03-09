"""Reporting commands."""

import click


@click.command("report")
@click.option("--period", default="30d", help="Report period (e.g., 7d, 30d, 90d)")
@click.option(
    "--format", type=click.Choice(["text", "json", "html", "pdf"]), default="text"
)
@click.pass_context
def report(ctx, period, format):
    """Generate backup activity and cost reports."""
    click.echo(f"Generating report - coming soon (period: {period}, format: {format})")
