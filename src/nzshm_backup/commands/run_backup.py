"""Manual backup execution command."""

from typing import Literal

import typer

from nzshm_backup.state import get_state

app = typer.Typer()


@app.callback(invoke_without_command=True)
def run(
    source: Literal["toshi", "ths", "all"] = typer.Option(
        "all", help="Data source to backup"
    ),
):
    """Execute manual backup.

    Triggers backup for specified source(s). Respects the global --dry-run flag.
    """
    state = get_state()
    if state.dry_run:
        typer.echo(f"[DRY RUN] Would trigger backup for: {source}")
    else:
        typer.echo(f"Starting backup for: {source} - implementation coming soon")
