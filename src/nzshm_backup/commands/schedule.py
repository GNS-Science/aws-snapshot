"""Schedule management commands."""

import click


@click.group("schedule")
def schedule():
    """Manage backup schedules."""
    pass


@schedule.command("show")
@click.pass_context
def show(ctx):
    """Show current backup schedules."""
    click.echo("Schedule management - coming soon")


@schedule.command("set")
@click.option("--frequency", type=click.Choice(["daily", "weekly"]), required=True)
@click.option("--source", type=click.Choice(["toshi", "ths", "all"]), required=True)
@click.option("--time", default="02:00", help="Time in HH:MM format (NZST)")
@click.pass_context
def set(ctx, frequency, source, time):
    """Set backup schedule frequency."""
    click.echo(f"Schedule set - coming soon: {frequency} for {source} at {time}")


@schedule.command("enable")
@click.argument("source")
@click.pass_context
def enable(ctx, source):
    """Enable backup schedule for a source."""
    click.echo(f"Enabling schedule for {source} - coming soon")


@schedule.command("disable")
@click.argument("source")
@click.pass_context
def disable(ctx, source):
    """Disable backup schedule for a source."""
    click.echo(f"Disabling schedule for {source} - coming soon")
