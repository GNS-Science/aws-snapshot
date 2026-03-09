"""Status command - current backup state."""

import click


@click.command("status")
@click.option("--source", type=click.Choice(["toshi", "ths", "all"]), default="all")
@click.option("--output", type=click.Choice(["text", "json", "yaml"]), default="text")
@click.pass_context
def status(ctx, source, output):
    """Show current backup status.

    Displays last backup time, next scheduled run, and overall health.
    """
    click.echo(f"Backup status - coming soon (source: {source}, format: {output})")
