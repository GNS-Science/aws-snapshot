"""Main CLI entry point for NSHM Backup Solution."""

import click
from . import __version__


@click.group()
@click.version_option(version=__version__)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without executing"
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["text", "json", "yaml"]),
    default="text",
    help="Output format",
)
@click.pass_context
def main(ctx, verbose, dry_run, output):
    """NSHM Backup Solution - Manage AWS backups for ToshiAPI and THS datasets.

    This tool provides backup scheduling, execution, restore, and cost management
    for NSHM data stored in S3 and DynamoDB.

    Examples:

        $ backup schedule show

        $ backup run --source toshi --dry-run

        $ backup restore list --limit 10

        $ backup status --output json
    """
    ctx.ensure_object(dict)
    ctx.obj["VERBOSE"] = verbose
    ctx.obj["DRY_RUN"] = dry_run
    ctx.obj["OUTPUT"] = output


# Import and register subcommand groups
from nzshm_backup.commands.schedule import schedule as schedule_group
from nzshm_backup.commands.run_backup import run
from nzshm_backup.commands.restore import restore
from nzshm_backup.commands.test import test as test_group
from nzshm_backup.commands.status import status
from nzshm_backup.commands.report import report
from nzshm_backup.commands.costs import costs as costs_group
from nzshm_backup.commands.config import config as config_group

main.add_command(schedule_group)
main.add_command(run)
main.add_command(restore)
main.add_command(test_group)
main.add_command(status)
main.add_command(report)
main.add_command(costs_group)
main.add_command(config_group)


def cli():
    """CLI entry point."""
    main()


if __name__ == "__main__":
    cli()
