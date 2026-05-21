"""``backup notifications`` CLI — manage SNS subscriptions from config.

backup-config.{stage}.yaml owns the recipient lists for both notification
topics; ``backup notifications apply`` reconciles each topic's actual SNS
subscriptions to match. Workflow:

    edit backup-config.production.yaml      (add/remove addresses)
    uv run backup notifications apply       (subscribe new, unsubscribe stale)

New subscribers receive an AWS confirmation email and must click the link
before delivery starts. Removing an address unsubscribes immediately
(no confirmation step for un-subscription).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import boto3
import typer

from nzshm_backup.config import load_config

app = typer.Typer(help="Manage SNS topic subscriptions for backup notifications.")


@dataclass
class _SubscriptionDiff:
    topic_arn: str
    topic_name: str
    to_add: list[str] = field(default_factory=list)
    to_remove: list[tuple[str, str]] = field(default_factory=list)  # (email, sub_arn)
    pending: list[str] = field(default_factory=list)  # confirmed-not-yet
    kept: list[str] = field(default_factory=list)


def _resolve_topic_arns(
    session: boto3.Session, stage: str
) -> tuple[str, str]:
    """Construct ARNs for the alerts + reports topics for a given stage."""
    region = session.region_name or "ap-southeast-2"
    account = session.client("sts").get_caller_identity()["Account"]
    return (
        f"arn:aws:sns:{region}:{account}:nzshm-backup-alerts-{stage}",
        f"arn:aws:sns:{region}:{account}:nzshm-backup-reports-{stage}",
    )


def _diff_subscriptions(
    sns_client, topic_arn: str, topic_name: str, desired: list[str]
) -> _SubscriptionDiff:
    """Compare desired email list to current SNS subscriptions on the topic."""
    diff = _SubscriptionDiff(topic_arn=topic_arn, topic_name=topic_name)
    desired_set = {e.strip().lower() for e in desired if e.strip()}

    current_emails: set[str] = set()
    current_pending: set[str] = set()
    arns_by_email: dict[str, str] = {}

    paginator = sns_client.get_paginator("list_subscriptions_by_topic")
    for page in paginator.paginate(TopicArn=topic_arn):
        for sub in page.get("Subscriptions", []):
            if sub.get("Protocol") != "email":
                continue
            endpoint = (sub.get("Endpoint") or "").strip().lower()
            sub_arn = sub.get("SubscriptionArn", "")
            if sub_arn == "PendingConfirmation":
                current_pending.add(endpoint)
                continue
            current_emails.add(endpoint)
            arns_by_email[endpoint] = sub_arn

    # to_add: desired but not present.
    # Skip if already pending — avoid duplicate confirmation emails.
    for email in sorted(desired_set - current_emails - current_pending):
        diff.to_add.append(email)

    # to_remove: present but no longer desired (pending ones cannot be unsubscribed individually)
    for email in sorted(current_emails - desired_set):
        diff.to_remove.append((email, arns_by_email[email]))

    diff.pending = sorted(current_pending & desired_set)
    diff.kept = sorted(current_emails & desired_set)
    return diff


def _apply_diff(sns_client, diff: _SubscriptionDiff, dry_run: bool) -> None:
    for email in diff.to_add:
        if dry_run:
            typer.echo(f"  + would subscribe {email}")
        else:
            sns_client.subscribe(
                TopicArn=diff.topic_arn, Protocol="email", Endpoint=email
            )
            typer.echo(f"  + subscribed {email}  (awaiting confirmation email)")

    for email, sub_arn in diff.to_remove:
        if dry_run:
            typer.echo(f"  - would unsubscribe {email}")
        else:
            sns_client.unsubscribe(SubscriptionArn=sub_arn)
            typer.echo(f"  - unsubscribed {email}")

    for email in diff.kept:
        typer.echo(f"  = {email}")

    for email in diff.pending:
        typer.echo(f"  ~ {email}  (still pending confirmation — no action)")


def _print_summary(channel: str, diff: _SubscriptionDiff) -> None:
    typer.echo(f"\n[{channel}] {diff.topic_name}")
    typer.echo(
        f"  topic: {diff.topic_arn}\n"
        f"  desired={len(diff.kept) + len(diff.to_add) + len(diff.pending)}  "
        f"current={len(diff.kept) + len(diff.to_remove) + len(diff.pending)}  "
        f"add={len(diff.to_add)}  remove={len(diff.to_remove)}  "
        f"pending={len(diff.pending)}"
    )


@app.command("apply")
def apply(
    stage: str = typer.Option(
        "prod", "--stage", help="Stage name (used to derive topic ARNs)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without modifying SNS"
    ),
    only: Literal["all", "alerts", "reports"] = typer.Option(
        "all", "--only", help="Reconcile only one channel (or 'all')"
    ),
) -> None:
    """Reconcile SNS subscriptions on both topics to match config.

    Reads ``notifications.alerts.emails`` and
    ``notifications.reports.email.addresses`` from ``backup-config.yaml``
    and converges each topic's email subscriptions.

    Pending confirmations are left alone — they cannot be cancelled or
    re-issued via API once sent; the recipient must click the link or
    let it expire (~3 days).
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    session = boto3.Session()
    sns_client = session.client("sns")
    alerts_arn, reports_arn = _resolve_topic_arns(session, stage)

    if only in ("all", "alerts"):
        alerts_diff = _diff_subscriptions(
            sns_client,
            alerts_arn,
            f"nzshm-backup-alerts-{stage}",
            config.notifications.alerts.emails,
        )
        _print_summary("alerts", alerts_diff)
        _apply_diff(sns_client, alerts_diff, dry_run)

    if only in ("all", "reports"):
        reports_diff = _diff_subscriptions(
            sns_client,
            reports_arn,
            f"nzshm-backup-reports-{stage}",
            config.notifications.reports.email.addresses,
        )
        _print_summary("reports", reports_diff)
        _apply_diff(sns_client, reports_diff, dry_run)

    typer.echo("")
    if dry_run:
        typer.echo("(dry-run: no changes applied)")
    else:
        typer.echo(
            "Subscriptions updated. New subscribers must click the "
            "confirmation email link before delivery starts."
        )


@app.command("show")
def show(
    stage: str = typer.Option("prod", "--stage"),
) -> None:
    """List current SNS subscriptions on both topics (read-only)."""
    session = boto3.Session()
    sns_client = session.client("sns")
    alerts_arn, reports_arn = _resolve_topic_arns(session, stage)

    for label, arn in (("alerts", alerts_arn), ("reports", reports_arn)):
        typer.echo(f"\n[{label}] {arn}")
        paginator = sns_client.get_paginator("list_subscriptions_by_topic")
        any_sub = False
        for page in paginator.paginate(TopicArn=arn):
            for sub in page.get("Subscriptions", []):
                any_sub = True
                state = (
                    "pending"
                    if sub.get("SubscriptionArn") == "PendingConfirmation"
                    else "confirmed"
                )
                typer.echo(f"  {sub.get('Protocol'):8} {sub.get('Endpoint'):40}  {state}")
        if not any_sub:
            typer.echo("  (no subscriptions)")
