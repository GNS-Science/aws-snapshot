"""``backup health-report`` CLI — exercise the daily-report path from a laptop.

Same code path the Lambda invokes on the EventBridge schedule
(``backup schedule add --task-type health_report``). This module is the
developer- and operator-facing way to verify Slack / SNS delivery
end-to-end after deploy, run ad-hoc previews, or reproduce an alert
under prod credentials.
"""

from __future__ import annotations

import os

import boto3
import typer

from nzshm_backup import health_report
from nzshm_backup.config import load_config

app = typer.Typer(help="Generate and (optionally) deliver the daily health report.")


def _resolve_reports_topic_arn(
    explicit: str | None, session: boto3.Session, stage: str
) -> str | None:
    """Determine the SNS reports topic ARN to publish to.

    Resolution order:
        1. ``--topic-arn`` flag (explicit override).
        2. ``$BACKUP_REPORTS_TOPIC_ARN`` env var (set by serverless on the Lambda).
        3. Constructed from session region + account + stage — matches the
           ``nzshm-backup-reports-{stage}`` name declared in serverless.yml.

    Returns None only if all three resolutions yield empty.
    """
    if explicit:
        return explicit
    env = os.environ.get("BACKUP_REPORTS_TOPIC_ARN")
    if env:
        return env
    region = session.region_name or "ap-southeast-2"
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except Exception:
        return None
    return f"arn:aws:sns:{region}:{account}:nzshm-backup-reports-{stage}"


@app.command("run")
def health_report_run(
    send: bool = typer.Option(
        False,
        "--send",
        help="Deliver via Slack + SNS per backup-config.{stage}.yaml notification flags. "
        "Without this flag, the report is printed locally only.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Skip the (slow) restore-test calls. Status + freshness + count-delta only.",
    ),
    weekday: int | None = typer.Option(
        None,
        "--weekday",
        help="Override the rotation weekday (0=Mon … 6=Sun). For testing the rotation logic.",
    ),
    topic_arn: str | None = typer.Option(
        None,
        "--topic-arn",
        help=(
            "SNS topic ARN for email delivery. If unset, falls back to "
            "$BACKUP_REPORTS_TOPIC_ARN (set by serverless on the Lambda), "
            "then to the deterministic name nzshm-backup-reports-{stage}."
        ),
    ),
    stage: str = typer.Option(
        "prod",
        "--stage",
        help="Deployment stage. Used only to derive the reports topic ARN "
        "when --topic-arn and $BACKUP_REPORTS_TOPIC_ARN are both unset.",
    ),
) -> None:
    """Build the daily health report and print it; optionally deliver via Slack + SNS."""
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    session = boto3.Session()

    # Build the report. --dry-run inhibits the (~30s per source) restore-test
    # calls; everything else runs the same.
    if dry_run:
        # Stub restore_test_source so the report still builds but doesn't
        # exercise the copy-and-verify path.
        from nzshm_backup import health_report as _hr

        original = _hr.restore_test_source
        try:
            _hr.restore_test_source = lambda **kw: None  # type: ignore[assignment]
            data = health_report.build_report(session, config, weekday=weekday)
        finally:
            _hr.restore_test_source = original
    else:
        data = health_report.build_report(session, config, weekday=weekday)

    # Print to stdout regardless — operator sees what was built.
    typer.echo(health_report.format_email_subject(data))
    typer.echo("")
    typer.echo(health_report.format_email_text(data))

    if not send:
        return

    resolved_topic = _resolve_reports_topic_arn(topic_arn, session, stage)
    delivery = health_report.send(
        data,
        config.notifications,
        session,
        reports_topic_arn=resolved_topic,
    )

    typer.echo("")
    typer.echo("Delivery:")
    slack_status = "ok" if delivery.slack_ok else (delivery.slack_error or "not attempted")
    typer.echo(f"  Slack: {slack_status}")
    sns_status = (
        f"ok (MessageId={delivery.sns_message_id})"
        if delivery.sns_ok
        else (delivery.sns_error or "not attempted")
    )
    typer.echo(f"  SNS:   {sns_status}")


@app.command("preview")
def health_report_preview(
    weekday: int | None = typer.Option(
        None, "--weekday", help="Override the rotation weekday (0=Mon … 6=Sun)."
    ),
) -> None:
    """Alias for ``run --dry-run`` — print without sending, skip restore tests."""
    health_report_run(send=False, dry_run=True, weekday=weekday, topic_arn=None, stage="prod")
