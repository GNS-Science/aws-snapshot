"""Main CLI entry point for NSHM Backup Solution."""

import typer
from typing import Optional
from nzshm_backup import __version__

app = typer.Typer(
    name="backup",
    help="NSHM Backup Solution - Manage AWS backups for ToshiAPI and THS datasets.",
    epilog="""
Examples:

    $ backup schedule show
    
    $ backup run --source toshi --dry-run
    
    $ backup restore list --limit 10
    
    $ backup status --output json
    """,
)


@app.callback()
def main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose output"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
    output: str = typer.Option("text", "--output", "-o", help="Output format"),
):
    """NSHM Backup Solution - Manage AWS backups for ToshiAPI and THS datasets."""
    pass


# Import and register subcommand groups
from nzshm_backup.commands.schedule import app as schedule_app
from nzshm_backup.commands.run_backup import app as run_app
from nzshm_backup.commands.restore import app as restore_app
from nzshm_backup.commands.test import app as test_app
from nzshm_backup.commands.status import app as status_app
from nzshm_backup.commands.report import app as report_app
from nzshm_backup.commands.costs import app as costs_app
from nzshm_backup.commands.config import app as config_app

app.add_typer(schedule_app, name="schedule", help="Manage backup schedules.")
app.add_typer(run_app, name="run", help="Execute manual backup.")
app.add_typer(restore_app, name="restore", help="Manage backup restores.")
app.add_typer(test_app, name="test", help="Run backup tests and validation.")
app.add_typer(status_app, name="status", help="Show current backup status.")
app.add_typer(report_app, name="report", help="Generate backup reports.")
app.add_typer(costs_app, name="costs", help="Manage and report backup costs.")
app.add_typer(config_app, name="config", help="Manage backup configuration.")


def cli():
    """CLI entry point."""
    app()


if __name__ == "__main__":
    cli()
