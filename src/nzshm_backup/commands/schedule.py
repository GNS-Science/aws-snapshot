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


@app.command("add")
def add_schedule(
    source: str = typer.Option(..., help="Data source"),
    frequency: Literal["daily", "weekly", "hourly", "minutely"] = typer.Option(
        ..., help="Backup frequency"
    ),
    time: str = typer.Option(
        "02:00",
        help=(
            "Time in HH:MM format (UTC). "
            "daily/weekly: both HH and MM are used. "
            "hourly: only MM is used (HH ignored). "
            "minutely: --time is ignored entirely."
        ),
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
):
    """Add (create or update) an EventBridge schedule rule for a backup source.

    Times must be specified in UTC. NZST is UTC+12, NZDT is UTC+13.
    """
    state = get_state()
    if dry_run:
        state.dry_run = True
    if frequency == "minutely":
        cron_expr = "rate(1 minute)"
    else:
        try:
            hh, mm = time.split(":")
            hh_int, mm_int = int(hh), int(mm)
        except ValueError:
            typer.echo(f"Error: Invalid time format '{time}'. Use HH:MM (UTC).", err=True)
            raise typer.Exit(1) from None

        if frequency == "daily":
            cron_expr = f"cron({mm_int} {hh_int} * * ? *)"
        elif frequency == "weekly":
            cron_expr = f"cron({mm_int} {hh_int} ? * SUN *)"
        else:  # hourly
            cron_expr = f"cron({mm_int} * * * ? *)"

    rule_name = _rule_name(source, frequency)
    session = boto3.Session()
    events = session.client("events")

    if state.dry_run:
        typer.echo(f"[DRY RUN] Would create/update rule '{rule_name}': {cron_expr}")
        return

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

    account_id = lambda_arn.split(":")[4]
    region = session.region_name or "ap-southeast-2"
    lambda_client = session.client("lambda")
    try:
        lambda_client.add_permission(
            FunctionName=lambda_arn,
            StatementId=f"AllowEventBridge-{rule_name}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{region}:{account_id}:rule/{rule_name}",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            pass  # permission already exists
        else:
            raise

    typer.echo(f"Target registered: {lambda_arn}")


@app.command("remove")
def remove_schedule(
    source: str = typer.Option(..., help="Data source"),
    frequency: Literal["daily", "weekly", "hourly", "minutely"] = typer.Option(
        ..., help="Backup frequency"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
):
    """Remove an EventBridge schedule rule and deregister its Lambda target."""
    state = get_state()
    if dry_run:
        state.dry_run = True
    rule_name = _rule_name(source, frequency)
    if state.dry_run:
        typer.echo(f"[DRY RUN] Would delete rule '{rule_name}' and its targets")
        return
    session = boto3.Session()
    events = session.client("events")

    # Remove Lambda target first (rule cannot be deleted while targets exist)
    try:
        events.remove_targets(Rule=rule_name, Ids=["backup-lambda"])
        typer.echo(f"Target removed: {rule_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            pass  # no targets or rule doesn't exist yet
        else:
            raise

    # Remove Lambda invoke permission
    try:
        config = load_config()
        lambda_arn = config.general.lambda_arn
    except FileNotFoundError:
        lambda_arn = None

    if lambda_arn:
        lambda_client = session.client("lambda")
        try:
            lambda_client.remove_permission(
                FunctionName=lambda_arn,
                StatementId=f"AllowEventBridge-{rule_name}",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ResourceNotFoundException", "NoSuchResourceException"):
                pass  # permission was never added
            else:
                raise

    # Delete the rule
    try:
        events.delete_rule(Name=rule_name)
        typer.echo(f"Rule deleted: {rule_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            typer.echo(f"Rule not found (skipping): {rule_name}")
        else:
            raise


@app.command("enable")
def enable(
    source: str = typer.Option(..., help="Data source"),
    frequency: str | None = typer.Option(
        None,
        help="Frequency to enable (daily, weekly, hourly, minutely). Defaults to all.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
):
    """Enable backup EventBridge schedule rule(s) for a source."""
    state = get_state()
    if dry_run:
        state.dry_run = True
    frequencies = [frequency] if frequency else ["daily", "weekly", "hourly", "minutely"]
    session = boto3.Session()
    events = session.client("events")

    for freq in frequencies:
        rule_name = _rule_name(source, freq)
        if state.dry_run:
            typer.echo(f"[DRY RUN] Would enable: {rule_name}")
            continue
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
    source: str = typer.Option(..., help="Data source"),
    frequency: str | None = typer.Option(
        None,
        help="Frequency to disable (daily, weekly, hourly, minutely). Defaults to all.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
):
    """Disable backup EventBridge schedule rule(s) for a source."""
    state = get_state()
    if dry_run:
        state.dry_run = True
    frequencies = [frequency] if frequency else ["daily", "weekly", "hourly", "minutely"]
    session = boto3.Session()
    events = session.client("events")

    for freq in frequencies:
        rule_name = _rule_name(source, freq)
        if state.dry_run:
            typer.echo(f"[DRY RUN] Would disable: {rule_name}")
            continue
        try:
            events.disable_rule(Name=rule_name)
            typer.echo(f"Disabled: {rule_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
                typer.echo(f"Rule not found (skipping): {rule_name}")
            else:
                raise
