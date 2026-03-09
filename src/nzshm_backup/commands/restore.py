"""Restore operations commands."""

import click


@click.group("restore")
def restore():
    """Manage backup restores."""
    pass


@restore.command("list")
@click.option("--source", type=click.Choice(["toshi", "ths"]))
@click.option("--limit", default=10, help="Number of restore points to show")
@click.pass_context
def list_restores(ctx, source, limit):
    """List available restore points."""
    click.echo(
        f"Listing restore points - coming soon (source: {source}, limit: {limit})"
    )


@restore.command("preview")
@click.option("--date", required=True, help="Backup date to restore (YYYY-MM-DD)")
@click.option("--source", type=click.Choice(["toshi", "ths"]))
@click.option("--target-bucket", help="Destination bucket for restore")
@click.pass_context
def preview(ctx, date, source, target_bucket):
    """Preview restore operation with cost estimate."""
    click.echo(f"Restore preview - coming soon: {date} for {source}")


@restore.command("run")
@click.option("--date", required=True, help="Backup date to restore (YYYY-MM-DD)")
@click.option("--source", type=click.Choice(["toshi", "ths"]))
@click.option("--target-bucket", help="Destination bucket for restore")
@click.option("--table", help="DynamoDB table to restore")
@click.option("--prefix", help="S3 prefix to restore (subset)")
@click.pass_context
def run_restore(ctx, date, source, target_bucket, table, prefix):
    """Execute restore operation."""
    click.echo(f"Restore execution - coming soon: {date}")


@restore.command("cancel")
@click.option("--job-id", required=True, help="Restore job ID to cancel")
@click.pass_context
def cancel(ctx, job_id):
    """Cancel in-progress restore."""
    click.echo(f"Cancelling restore job {job_id} - coming soon")
