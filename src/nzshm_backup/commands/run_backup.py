"""Manual backup execution command."""

import click


@click.command("run")
@click.option("--source", type=click.Choice(["toshi", "ths", "all"]), default="all")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without executing"
)
@click.pass_context
def run(ctx, source, dry_run):
    """Execute manual backup.

    Triggers backup for specified source(s). Use --dry-run to preview actions.
    """
    if dry_run:
        click.echo(f"[DRY RUN] Would trigger backup for: {source}")
    else:
        click.echo(f"Starting backup for: {source} - implementation coming soon")
