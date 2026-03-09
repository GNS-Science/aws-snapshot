"""Schedule management commands."""

from typing import Literal

import typer

app = typer.Typer()


@app.command("show")
def show():
    """Show current backup schedules."""
    typer.echo("Schedule management - coming soon")


@app.command("set")
def set_schedule(
    frequency: Literal["daily", "weekly"] = typer.Option(..., help="Backup frequency"),
    source: Literal["toshi", "ths", "all"] = typer.Option(..., help="Data source"),
    time: str = typer.Option("02:00", help="Time in HH:MM format (NZST)"),
):
    """Set backup schedule frequency."""
    typer.echo(f"Schedule set - coming soon: {frequency} for {source} at {time}")


@app.command("enable")
def enable(
    source: str = typer.Argument(..., help="Source to enable"),
):
    """Enable backup schedule for a source."""
    typer.echo(f"Enabling schedule for {source} - coming soon")


@app.command("disable")
def disable(
    source: str = typer.Argument(..., help="Source to disable"),
):
    """Disable backup schedule for a source."""
    typer.echo(f"Disabling schedule for {source} - coming soon")
