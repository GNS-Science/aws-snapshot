"""Schedule management commands (EventBridge)."""

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

import boto3
import typer
from botocore.exceptions import ClientError

from nzshm_backup.config.loader import load_config
from nzshm_backup.state import get_state
from nzshm_backup.time_utils import parse_datetime

app = typer.Typer()


def _rule_name(source: str, frequency: str) -> str:
    return f"nzshm-backup-{source}-{frequency}"


# EventBridge weekday abbreviations indexed by Python isoweekday() (1=Mon…7=Sun)
_EB_WEEKDAY = {1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT", 7: "SUN"}
_EB_TO_ISO = {v: k for k, v in _EB_WEEKDAY.items()}


def _schedule_expr_local_desc(expr: str) -> str:
    """Parse an EventBridge schedule expression and return a localised description.

    Handles:
    - ``cron(MM HH * * ? *)``       — daily
    - ``cron(MM HH ? * DAY *)``     — weekly
    - ``cron(MM * * * ? *)``        — hourly (at :MM past each hour)
    - ``rate(...)``                 — returned unchanged
    """
    import re

    if expr.startswith("rate("):
        return ""
    m = re.match(r"cron\((\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\)", expr)
    if not m:
        return ""
    mm_s, hh_s, dom, month, dow, year = m.groups()
    # Hourly: HH field is '*'
    if hh_s == "*":
        try:
            mm = int(mm_s)
            return _human_schedule_desc("hourly", 0, mm, None)
        except ValueError:
            return ""
    # Daily or weekly
    try:
        hh, mm = int(hh_s), int(mm_s)
    except ValueError:
        return ""
    if dow != "?":
        # weekly — dow is an EB weekday abbreviation (MON…SUN)
        return _human_schedule_desc("weekly", hh, mm, dow if dow in _EB_TO_ISO else None)
    return _human_schedule_desc("daily", hh, mm, None)


def _human_schedule_desc(frequency: str, hh_utc: int, mm_utc: int, weekday_utc: str | None) -> str:
    """Return a human-readable localised description of when the schedule fires."""
    now_utc = datetime.now(timezone.utc)

    if frequency == "weekly" and weekday_utc:
        # Find the next (or current) occurrence of weekday_utc
        target_iso = _EB_TO_ISO[weekday_utc]
        days_ahead = (target_iso - now_utc.isoweekday()) % 7
        anchor = (now_utc + timedelta(days=days_ahead)).replace(
            hour=hh_utc, minute=mm_utc, second=0, microsecond=0
        )
        local = anchor.astimezone()
        return f"→ {local.strftime('%A')} {local.strftime('%H:%M %Z')} locally"

    if frequency in ("daily", "weekly"):
        anchor = now_utc.replace(hour=hh_utc, minute=mm_utc, second=0, microsecond=0)
        local = anchor.astimezone()
        return f"→ {local.strftime('%H:%M %Z')} locally"

    if frequency == "hourly":
        anchor = now_utc.replace(hour=hh_utc, minute=mm_utc, second=0, microsecond=0)
        local = anchor.astimezone()
        return f"→ :{local.strftime('%M')} past each hour ({local.strftime('%Z')})"

    return ""


def _parse_schedule_time(time_str: str) -> tuple[int, int, str | None]:
    """Parse a time string and return (hour_utc, minute_utc, eb_weekday_or_none).

    ``eb_weekday`` is the EventBridge day abbreviation (e.g. ``"SAT"``) derived
    from the UTC datetime when the input includes a date component; ``None`` when
    only a time was supplied (caller should use a default day).

    Accepts:
    - ``HH:MM``               — treated as UTC; weekday=None
    - ``HH:MM TZ``            — e.g. ``12:15 NZDT``; weekday=None
    - ``YYYY-MM-DD HH:MM TZ`` — full datetime; weekday taken from UTC result
    - ISO 8601 datetime       — full datetime; weekday taken from UTC result
    """
    ts = time_str.strip()
    has_date = len(ts) >= 10 and ts[:4].isdigit() and ts[4:5] == "-"

    # Plain HH:MM — treat as UTC directly
    parts = ts.split(":")
    if len(parts) == 2 and " " not in ts:
        try:
            hh, mm = int(parts[0]), int(parts[1])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError(f"Time out of range: {ts!r}")
            return hh, mm, None
        except ValueError as e:
            if "out of range" in str(e):
                raise
    # Localised formats — delegate to parse_datetime and convert to UTC
    try:
        dt = parse_datetime(ts)
        dt_utc = dt.astimezone(timezone.utc)
        weekday = _EB_WEEKDAY[dt_utc.isoweekday()] if has_date else None
        return dt_utc.hour, dt_utc.minute, weekday
    except ValueError:
        pass
    raise ValueError(
        f"Invalid time {ts!r}. "
        "Use HH:MM (UTC), 'HH:MM TZ' (e.g. '12:15 NZDT'), "
        "or a full datetime (e.g. '2026-03-29 12:15 NZDT')."
    )


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

    typer.echo(f"{'Rule Name':<45} {'State':<10} {'Schedule':<30} {'Local time'}")
    typer.echo("-" * 100)
    for rule in rules:
        expr = rule.get("ScheduleExpression", "n/a")
        local = _schedule_expr_local_desc(expr)
        typer.echo(f"{rule['Name']:<45} {rule.get('State', 'UNKNOWN'):<10} " f"{expr:<30} {local}")


@app.command("add")
def add_schedule(
    source: str = typer.Option(..., help="Data source"),
    frequency: Literal["daily", "weekly", "hourly", "minutely"] = typer.Option(
        ..., help="Backup frequency"
    ),
    time: str = typer.Option(
        "02:00",
        help=(
            "Time for the schedule, converted to UTC. "
            "Accepts: HH:MM (UTC), 'HH:MM TZ' (e.g. '12:15 NZDT'), "
            "or a full datetime (e.g. '2026-03-29 12:15 NZDT'). "
            "For weekly schedules, the date determines the day-of-week (in UTC). "
            "daily/weekly: both HH and MM are used. "
            "hourly: only MM is used (HH ignored). "
            "minutely: --time is ignored entirely."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
):
    """Add (create or update) an EventBridge schedule rule for a backup source.

    Times are converted to UTC. For weekly schedules, pass a full datetime
    (e.g. '2026-03-29 12:15 NZDT') so the correct UTC day-of-week is used.
    """
    state = get_state()
    if dry_run:
        state.dry_run = True
    if frequency == "minutely":
        cron_expr = "rate(1 minute)"
    else:
        try:
            hh_int, mm_int, weekday = _parse_schedule_time(time)
        except ValueError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1) from None

        day: str | None = None
        if frequency == "daily":
            cron_expr = f"cron({mm_int} {hh_int} * * ? *)"
        elif frequency == "weekly":
            day = weekday or "SUN"  # default to Sunday if no date provided
            cron_expr = f"cron({mm_int} {hh_int} ? * {day} *)"
        else:  # hourly
            cron_expr = f"cron({mm_int} * * * ? *)"

    rule_name = _rule_name(source, frequency)
    session = boto3.Session()
    events = session.client("events")

    human = (
        ""
        if frequency == "minutely"
        else _human_schedule_desc(frequency, hh_int, mm_int, day if frequency == "weekly" else None)
    )

    if state.dry_run:
        typer.echo(f"[DRY RUN] Would create/update rule '{rule_name}': {cron_expr}  {human}")
        return

    events.put_rule(
        Name=rule_name,
        ScheduleExpression=cron_expr,
        State="ENABLED",
        Description=f"NSHM backup schedule: {source} {frequency} at {time} UTC",
    )
    typer.echo(f"Rule '{rule_name}' created/updated: {cron_expr}  {human}")

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
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
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
            if e.response["Error"]["Code"] in (
                "ResourceNotFoundException",
                "NoSuchResourceException",
            ):
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
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
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
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
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
