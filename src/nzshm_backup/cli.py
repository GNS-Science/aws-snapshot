"""Main CLI entry point for NSHM Backup Solution."""

import os

import typer

from nzshm_backup import __version__
from nzshm_backup.state import _state

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
    invoke_without_command=True,
)


def _check_aws_credential_conflict() -> None:
    """Warn if AWS_ACCESS_KEY_ID and AWS_PROFILE are both set.

    boto3 gives explicit credential env vars higher priority than AWS_PROFILE,
    so setting both (e.g. after eval-exporting creds then switching profile) will
    silently use the wrong account.
    """
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_PROFILE"):
        profile = os.environ["AWS_PROFILE"]
        typer.echo(
            f"Warning: AWS_ACCESS_KEY_ID is set and will override AWS_PROFILE={profile!r}.\n"
            "  The backup CLI will use the exported credentials, not the profile.\n"
            "  To fix, run:\n"
            "    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN",
            err=True,
        )


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
    output: str = typer.Option("text", "--output", "-o", help="Output format (text, json, yaml)"),
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
):
    """NSHM Backup Solution - Manage AWS backups for ToshiAPI and THS datasets."""
    if version:
        typer.echo(f"backup {__version__}")
        raise typer.Exit()

    _check_aws_credential_conflict()
    _state.verbose = verbose
    _state.dry_run = dry_run
    _state.output = output


# Import and register subcommand groups (must be after main() to avoid circular imports)
from nzshm_backup.commands.config import app as config_app  # noqa: E402
from nzshm_backup.commands.costs import app as costs_app  # noqa: E402
from nzshm_backup.commands.report import app as report_app  # noqa: E402
from nzshm_backup.commands.restore import app as restore_app  # noqa: E402
from nzshm_backup.commands.run_backup import app as run_app  # noqa: E402
from nzshm_backup.commands.schedule import app as schedule_app  # noqa: E402
from nzshm_backup.commands.status import app as status_app  # noqa: E402
from nzshm_backup.commands.test import app as test_app  # noqa: E402

app.add_typer(schedule_app, name="schedule", help="Manage backup schedules.")
app.add_typer(run_app, name="run", help="Execute manual backup.")
app.add_typer(restore_app, name="restore", help="Manage backup restores. (TODO)")
app.add_typer(test_app, name="test", help="Run backup tests and validation. (TODO)")
app.add_typer(status_app, name="status", help="Show current backup status.")
app.add_typer(report_app, name="report", help="Generate backup reports. (TODO)")
app.add_typer(costs_app, name="costs", help="Manage and report backup costs. (TODO)")
app.add_typer(config_app, name="config", help="Manage backup configuration.")


def cli():
    """CLI entry point."""
    app()


import typer.main as _typer_main  # noqa: E402

click_app = _typer_main.get_command(app)

if __name__ == "__main__":
    cli()
