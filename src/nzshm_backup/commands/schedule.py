"""Schedule management commands (EventBridge)."""

import json
from typing import Literal

import boto3
import typer
from botocore.exceptions import ClientError

from nzshm_backup.config.loader import load_config
from nzshm_backup.state import get_state

app = typer.Typer()


def _rule_name(source: str, frequency: str) -> str:
    return f"nzshm-backup-{source}-{frequency}"


@app.command("show")
def show():
    """Show current backup schedules (EventBridge rules with prefix nzshm-backup-)."""
    state = get_state()
    session = boto3.Session()
    events = session.client("events")

    rules = []
    paginator = events.get_paginator("list_rules")
    for page in paginator.paginate(NamePrefix="nzshm-backup-"):
        rules.extend(page.get("Rules", []))

    if not rules:
        typer.echo("No backup schedules found.")
        return

    if state.output == "json":
        typer.echo(json.dumps(rules, indent=2))
        return

    typer.echo(f"{'Rule Name':<45} {'State':<10} {'Schedule'}")
    typer.echo("-" * 80)
    for rule in rules:
        typer.echo(
            f"{rule['Name']:<45} {rule.get('State', 'UNKNOWN'):<10} "
            f"{rule.get('ScheduleExpression', 'n/a')}"
        )


@app.command("set")
def set_schedule(
    source: Literal["toshi", "ths", "all"] = typer.Option(..., help="Data source"),
    frequency: Literal["daily", "weekly"] = typer.Option(..., help="Backup frequency"),
    time: str = typer.Option(
        "02:00",
        help="Time in HH:MM format (UTC). Convert from NZST/NZDT manually.",
    ),
):
    """Set (create or update) an EventBridge schedule rule for a backup source.

    Times must be specified in UTC. NZST is UTC+12, NZDT is UTC+13.
    """
    try:
        hh, mm = time.split(":")
        hh_int, mm_int = int(hh), int(mm)
    except ValueError:
        typer.echo(f"Error: Invalid time format '{time}'. Use HH:MM (UTC).", err=True)
        raise typer.Exit(1) from None

    if frequency == "daily":
        cron_expr = f"cron({mm_int} {hh_int} * * ? *)"
    else:  # weekly (Sunday)
        cron_expr = f"cron({mm_int} {hh_int} ? * SUN *)"

    rule_name = _rule_name(source, frequency)
    session = boto3.Session()
    events = session.client("events")

    events.put_rule(
        Name=rule_name,
        ScheduleExpression=cron_expr,
        State="ENABLED",
        Description=f"NSHM backup schedule: {source} {frequency} at {time} UTC",
    )
    typer.echo(f"Rule '{rule_name}' created/updated: {cron_expr}")

    try:
        config = load_config()
        lambda_arn = config.general.lambda_arn
    except FileNotFoundError:
        lambda_arn = None

    if not lambda_arn:
        typer.echo(
            "Warning: lambda_arn not configured — rule created but no target registered.",
            err=True,
        )
        return

    events.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "backup-lambda",
                "Arn": lambda_arn,
                "Input": json.dumps({"source": source, "trigger_type": "scheduled"}),
            }
        ],
    )

    lambda_client = session.client("lambda")
    try:
        lambda_client.add_permission(
            FunctionName=lambda_arn,
            StatementId=f"AllowEventBridge-{rule_name}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{session.region_name or 'ap-southeast-2'}:"
            f"*:rule/{rule_name}",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            pass  # permission already exists
        else:
            raise

    typer.echo(f"Target registered: {lambda_arn}")


@app.command("enable")
def enable(
    source: str = typer.Argument(..., help="Source to enable (e.g. toshi, ths, all)"),
    frequency: str | None = typer.Option(
        None, help="Frequency to enable (daily or weekly). Defaults to both."
    ),
):
    """Enable backup EventBridge schedule rule(s) for a source."""
    frequencies = [frequency] if frequency else ["daily", "weekly"]
    session = boto3.Session()
    events = session.client("events")

    for freq in frequencies:
        rule_name = _rule_name(source, freq)
        try:
            events.enable_rule(Name=rule_name)
            typer.echo(f"Enabled: {rule_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
                typer.echo(f"Rule not found (skipping): {rule_name}")
            else:
                raise


@app.command("disable")
def disable(
    source: str = typer.Argument(..., help="Source to disable (e.g. toshi, ths, all)"),
    frequency: str | None = typer.Option(
        None, help="Frequency to disable (daily or weekly). Defaults to both."
    ),
):
    """Disable backup EventBridge schedule rule(s) for a source."""
    frequencies = [frequency] if frequency else ["daily", "weekly"]
    session = boto3.Session()
    events = session.client("events")

    for freq in frequencies:
        rule_name = _rule_name(source, freq)
        try:
            events.disable_rule(Name=rule_name)
            typer.echo(f"Disabled: {rule_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
                typer.echo(f"Rule not found (skipping): {rule_name}")
            else:
                raise
