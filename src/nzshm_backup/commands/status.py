"""Status command - current backup state."""

import json
from typing import Literal

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.s3_backup import get_cross_account_session

app = typer.Typer()

EXPORT_LIMIT = 5  # most recent exports to show per table


def _get_recent_exports(dynamodb_client, table_arn: str, limit: int = EXPORT_LIMIT) -> list[dict]:
    """Return the most recent exports for a table, newest first."""
    exports = []
    kwargs: dict = {"TableArn": table_arn, "MaxResults": 25}
    while True:
        response = dynamodb_client.list_exports(**kwargs)
        exports.extend(response.get("ExportSummaries", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    exports.sort(key=lambda e: e.get("ExportTime") or "", reverse=True)
    return exports[:limit]


def _print_source_status(source_alias: str, source_config, session: boto3.Session) -> None:
    source_session = (
        get_cross_account_session(session, source_config.source_account_role_arn)
        if source_config.source_account_role_arn
        else session
    )
    dynamodb_client = source_session.client("dynamodb")

    typer.echo(f"\n[{source_alias}] {source_config.display_name}")

    if not source_config.dynamodb_tables:
        typer.echo("  DynamoDB: no tables configured")
    else:
        typer.echo("  DynamoDB exports:")
        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            try:
                exports = _get_recent_exports(dynamodb_client, table_arn)
                if not exports:
                    typer.echo(f"    {table_name}: no exports found")
                    continue
                latest = exports[0]
                status = latest["ExportStatus"]
                status_icon = {"COMPLETED": "✓", "IN_PROGRESS": "⋯", "FAILED": "✗"}.get(status, "?")

                detail = dynamodb_client.describe_export(ExportArn=latest["ExportArn"])
                desc = detail["ExportDescription"]
                start_time = desc.get("StartTime")
                ts = f"  [{start_time.strftime('%Y-%m-%d %H:%M UTC')}]" if start_time else ""
                typer.echo(f"    {status_icon} {table_name}: {status}{ts}")

                if status == "FAILED":
                    msg = desc.get("FailureMessage", "")
                    if "because" in msg:
                        msg = msg.split("because")[-1].strip()
                    typer.echo(f"      reason: {msg[:120]}")
            except Exception as e:
                typer.echo(f"    {table_name}: error fetching status ({e})")

    if not source_config.s3_buckets:
        typer.echo("  S3: no buckets configured")
    else:
        typer.echo(f"  S3 buckets: {len(source_config.s3_buckets)} configured")


@app.callback(invoke_without_command=True)
def status(
    source: str = typer.Option("all", help="Source name from config, or 'all'"),
    output: Literal["text", "json"] = typer.Option("text", help="Output format"),
):
    """Show current backup status.

    Displays recent DynamoDB export status per table for each source.
    Cross-account sources are queried transparently via the configured reader role.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source != "all" and source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    sources_to_check = (
        list(config.sources.keys()) if source == "all" else [source]
    )

    if output == "json":
        _print_json_status(sources_to_check, config)
        return

    session = boto3.Session()
    for alias in sources_to_check:
        _print_source_status(alias, config.sources[alias], session)
    typer.echo("")


def _print_json_status(sources: list[str], config) -> None:
    session = boto3.Session()
    out = {}
    for alias in sources:
        source_config = config.sources[alias]
        source_session = (
            get_cross_account_session(session, source_config.source_account_role_arn)
            if source_config.source_account_role_arn
            else session
        )
        dynamodb_client = source_session.client("dynamodb")
        tables = {}
        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            try:
                exports = _get_recent_exports(dynamodb_client, table_arn)
                tables[table_name] = [
                    {
                        "export_arn": e["ExportArn"],
                        "status": e["ExportStatus"],
                        "export_time": str(e.get("ExportTime", "")),
                    }
                    for e in exports
                ]
            except Exception as e:
                tables[table_name] = {"error": str(e)}
        out[alias] = {"dynamodb_tables": tables}
    typer.echo(json.dumps(out, indent=2))
