"""Backup event log commands."""

from datetime import datetime, timezone

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.event_log import read_events
from nzshm_backup.s3_backup import get_account_id

app = typer.Typer()

_EVENT_ICONS = {
    "backup_run": "·",
    "backup_run_complete": "📦",
    "restore_submitted": "⬇",
    "restore_completed": "✓",
    "pitr_reenabled": "🔒",
    "test_restore": "🧪",
}


def _fmt_dt(dt) -> str:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


@app.callback(invoke_without_command=True)
def events(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    limit: int = typer.Option(20, "--limit", help="Maximum number of events to show"),
    since: str | None = typer.Option(
        None, "--since", help="Show events on or after this date (YYYY-MM-DD or ISO datetime)"
    ),
):
    """Show the backup/restore event log for a source.

    Events are stored in the backup bucket under _events/YYYY-MM/events.jsonl.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    source_config = config.sources[source]
    if not source_config.s3_buckets:
        typer.echo("Error: source has no S3 buckets configured — no event log available.", err=True)
        raise typer.Exit(1)

    session = boto3.Session()
    account_id = get_account_id(session)
    source_account_id = source_config.source_account_id or account_id

    backup_bucket = source_config.get_backup_bucket_name(
        source_config.s3_buckets[0].label, config.general.region, source_account_id, source
    )

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            typer.echo(
                f"Error: invalid --since value '{since}'. Use YYYY-MM-DD or ISO datetime.", err=True
            )
            raise typer.Exit(1) from None

    evts = read_events(session, backup_bucket, source=source, since=since_dt, limit=limit)

    if not evts:
        typer.echo(f"No events found for '{source}' in s3://{backup_bucket}/_events/")
        return

    typer.echo(f"\n[{source}] event log (s3://{backup_bucket}/_events/):\n")
    for evt in evts:
        icon = _EVENT_ICONS.get(evt.get("event_type", ""), "·")
        ts = _fmt_dt(evt.get("timestamp", ""))
        event_type = evt.get("event_type", "unknown")
        details = evt.get("details", {})

        # Format a concise summary line per event type
        if event_type == "backup_run":
            target = details.get("bucket") or details.get("table", "?")
            status = details.get("status", "?")
            mode = details.get("mode", "")
            extra = ""
            if details.get("objects_copied") is not None:
                extra = f"  {details['objects_copied']} objects"
            elif details.get("objects_in_manifest") is not None:
                extra = f"  {details['objects_in_manifest']} objects in manifest"
            elif details.get("export_arn"):
                extra = f"  {details['export_arn'].split('/')[-1]}"
            typer.echo(f"  {icon} {ts}  backup_run  {target}  [{mode}] {status}{extra}")

        elif event_type == "restore_submitted":
            table = details.get("dest_table", "?")
            pt = details.get("restore_point", "")
            triggered = details.get("triggered_by", "")
            pt_fmt = _fmt_dt(pt) if pt else ""
            typer.echo(f"  {icon} {ts}  restore_submitted  {table}  as-at {pt_fmt}  ({triggered})")

        elif event_type == "restore_completed":
            table = details.get("dest_table", "?")
            typer.echo(f"  {icon} {ts}  restore_completed  {table}")

        elif event_type == "pitr_reenabled":
            table = details.get("table_arn", "?").split("/")[-1]
            typer.echo(f"  {icon} {ts}  pitr_reenabled  {table}")

        elif event_type == "backup_run_complete":
            ok = "✓" if details.get("success") else "✗"
            s3 = details.get("s3_buckets", 0)
            ddb = details.get("dynamodb_tables", 0)
            errors = details.get("errors", [])
            err_str = f"  {len(errors)} error(s)" if errors else ""
            typer.echo(
                f"  {icon} {ts}  backup_run_complete  {ok}"
                f"  {s3} S3 bucket(s)  {ddb} DynamoDB table(s){err_str}"
            )

        elif event_type == "test_restore":
            bucket = details.get("bucket", "?")
            result = details.get("result", "?")
            typer.echo(f"  {icon} {ts}  test_restore  {bucket}  {result}")

        else:
            typer.echo(f"  {icon} {ts}  {event_type}  {details}")

    typer.echo("")
