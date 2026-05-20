"""Daily health-report orchestrator (ADR-005 slow path).

Builds a per-source picture combining:

- backup state from ``commands.status.get_status_dict``
- inventory freshness from ``inventory_state.inventory_health_for_bucket_pair``
- object-count delta from ``athena_inventory.count_delta`` (ADR-006 mit. 1)
- restore verification from ``commands.test.restore_test_source`` (weka
  daily canary + rotating large source Mon/Wed/Fri)
- DynamoDB PITR status (queried directly here; small enough that an
  extraction wouldn't pay back)

Formats the result for Slack (Block Kit) and plain-text email (SNS),
honouring ``notifications.{slack,reports}.enabled`` for delivery.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

import boto3

from nzshm_backup.athena_inventory import count_delta
from nzshm_backup.commands.status import get_status_dict
from nzshm_backup.commands.test import RestoreTestResult, restore_test_source
from nzshm_backup.inventory_state import inventory_health_for_bucket_pair
from nzshm_backup.notifications.slack import (
    SlackDeliveryError,
    resolve_webhook_url,
    send_slack,
)
from nzshm_backup.notifications.sns import SnsDeliveryError, publish_report
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.time_utils import nz_today

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------

_CANARY_SOURCE = "weka"
# Weekday → large source to restore-test that day. Other weekdays only
# the canary runs. weekday(): Mon=0, Tue=1, ..., Sun=6.
_ROTATION_BY_WEEKDAY: dict[int, str] = {
    0: "ths",      # Monday
    2: "toshi",    # Wednesday
    4: "static",   # Friday
}

# An inventory report older than this is considered stale (ADR-007 mit. 4).
_FRESHNESS_THRESHOLD_HOURS = 30.0

# Delta drop must clear BOTH thresholds to be quiet; cross either to alert.
# (ADR-006 mitigation 1: catch large source deletions.)
_DELTA_PCT_THRESHOLD = -5.0       # i.e. drop of 5% or more
_DELTA_ABS_THRESHOLD = -10_000    # i.e. drop of 10k+ objects

Status = Literal["green", "yellow", "red"]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SourceHealthData:
    alias: str
    status_data: dict[str, Any]
    inventory_age_hours: float | None  # None if no inventory present
    inventory_stale: bool
    count_delta: dict[str, Any] | None  # None if delta unavailable
    delta_flag: bool                    # True if a notable drop was seen
    restore_test: RestoreTestResult | None  # None if not tested this run
    pitr_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall: Status = "green"
    notes: list[str] = field(default_factory=list)


@dataclass
class HealthReportData:
    report_date: date
    sources: list[SourceHealthData] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def overall(self) -> Status:
        if any(s.overall == "red" for s in self.sources):
            return "red"
        if any(s.overall == "yellow" for s in self.sources):
            return "yellow"
        return "green"

    @property
    def healthy_count(self) -> int:
        return sum(1 for s in self.sources if s.overall == "green")


# ---------------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------------


def _check_dynamodb_pitr(
    session: boto3.Session,
    source_config,
    source_account_role_arn: str | None,
) -> dict[str, dict[str, Any]]:
    """Per-table PITR + export-bucket reachability dict."""
    if not source_config.dynamodb_tables:
        return {}
    inner = (
        get_cross_account_session(session, source_account_role_arn)
        if source_account_role_arn
        else session
    )
    dynamo = inner.client("dynamodb")
    out: dict[str, dict[str, Any]] = {}
    for table_arn in source_config.dynamodb_tables:
        table = table_arn.split("/")[-1]
        try:
            resp = dynamo.describe_continuous_backups(TableName=table)
            pitr = resp["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
            out[table] = {
                "enabled": pitr.get("PointInTimeRecoveryStatus") == "ENABLED",
                "latest_restorable": pitr.get("LatestRestorableDateTime"),
            }
        except Exception as e:
            out[table] = {"enabled": False, "error": str(e)}
    return out


def _classify_source(s: SourceHealthData) -> Status:
    """Map signals → green / yellow / red.

    - red: restore-test failure, PITR disabled on any configured DDB table,
      inventory missing entirely, or a notable count drop (delta_flag).
    - yellow: inventory stale but present.
    - green: otherwise.
    """
    if s.restore_test and s.restore_test.overall == "failed":
        return "red"
    if any(not t.get("enabled") for t in s.pitr_tables.values()):
        return "red"
    if s.inventory_age_hours is None:
        return "red"
    if s.delta_flag:
        return "red"
    if s.inventory_stale:
        return "yellow"
    return "green"


def build_report(
    session: boto3.Session,
    config,
    today: date | None = None,
    weekday: int | None = None,
) -> HealthReportData:
    """Gather all per-source signals and return a HealthReportData.

    Args:
        weekday: Override the weekday for rotation testing (0=Mon, 6=Sun).
            Defaults to ``today.weekday()``.
    """
    started = time.monotonic()
    today = today or nz_today()
    weekday = weekday if weekday is not None else today.weekday()

    aliases = list(config.sources.keys())
    status_data = get_status_dict(aliases, config, session)
    account_id = get_account_id(session)

    rotated = _ROTATION_BY_WEEKDAY.get(weekday)
    sources_to_restore_test = {_CANARY_SOURCE}
    if rotated and rotated in config.sources:
        sources_to_restore_test.add(rotated)

    report = HealthReportData(report_date=today)
    now_utc = datetime.now(timezone.utc)

    for alias in aliases:
        source_config = config.sources[alias]
        source_account_id = source_config.source_account_id or account_id
        notes: list[str] = []

        # Inventory freshness
        inv_age: float | None = None
        inv_stale = False
        if source_config.s3_buckets:
            bucket_cfg = source_config.s3_buckets[0]
            source_bucket = bucket_cfg.arn.split(":::")[-1]
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_cfg.label, config.general.region, source_account_id, alias
            )
            source_session = (
                get_cross_account_session(session, source_config.source_account_role_arn)
                if source_config.source_account_role_arn
                else session
            )
            try:
                inv = inventory_health_for_bucket_pair(
                    session, source_session, alias, source_bucket, backup_bucket
                )
                effective = inv.get("effective_data_ts")
                if effective:
                    inv_age = (now_utc - effective).total_seconds() / 3600.0
                    inv_stale = inv_age > _FRESHNESS_THRESHOLD_HOURS
                else:
                    notes.append("no inventory data available")
            except Exception as e:
                notes.append(f"inventory health check failed: {e}")

        # Object-count delta (ADR-006 mitigation 1) — only source side; the
        # backup side is supposed to mirror but won't catch the user-deletion
        # case.
        delta: dict[str, Any] | None = None
        delta_flag = False
        if source_config.s3_buckets:
            bucket_cfg = source_config.s3_buckets[0]
            source_bucket = bucket_cfg.arn.split(":::")[-1]
            try:
                delta = count_delta(session, alias, "source", source_bucket)
                if delta.get("available"):
                    abs_drop = delta.get("delta") or 0
                    pct_drop = delta.get("delta_pct") or 0
                    if abs_drop <= _DELTA_ABS_THRESHOLD or pct_drop <= _DELTA_PCT_THRESHOLD:
                        delta_flag = True
                        notes.append(
                            f"object count dropped by {abs_drop:,} "
                            f"({pct_drop:.1f}%) vs yesterday"
                        )
            except Exception as e:
                notes.append(f"count delta check failed: {e}")

        # Restore verification (canary + rotation)
        restore_result: RestoreTestResult | None = None
        if alias in sources_to_restore_test:
            try:
                restore_result = restore_test_source(
                    session=session,
                    config=config,
                    source_alias=alias,
                    sample_size=10,
                    use_batch=False,
                    emit_events=True,
                )
            except Exception as e:
                notes.append(f"restore test exception: {e}")

        pitr = _check_dynamodb_pitr(
            session, source_config, source_config.source_account_role_arn
        )

        src = SourceHealthData(
            alias=alias,
            status_data=status_data.get(alias, {}),
            inventory_age_hours=inv_age,
            inventory_stale=inv_stale,
            count_delta=delta,
            delta_flag=delta_flag,
            restore_test=restore_result,
            pitr_tables=pitr,
            notes=notes,
        )
        src.overall = _classify_source(src)
        report.sources.append(src)

    report.duration_seconds = time.monotonic() - started
    return report


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


_STATUS_ICON = {"green": "✓", "yellow": "⚠", "red": "✗"}
_STATUS_TEXT = {"green": "GREEN", "yellow": "YELLOW", "red": "RED"}


def format_email_subject(data: HealthReportData) -> str:
    n = data.healthy_count
    total = len(data.sources)
    return (
        f"NSHM backup health {data.report_date.isoformat()} — "
        f"{_STATUS_TEXT[data.overall]} ({n}/{total})"
    )


def format_email_text(data: HealthReportData) -> str:
    lines: list[str] = []
    n = data.healthy_count
    total = len(data.sources)
    lines.append(f"NSHM Backup Health Report — {data.report_date.isoformat()}")
    lines.append("")
    lines.append(f"Overall: {_STATUS_TEXT[data.overall]}  ({n}/{total} sources healthy)")
    lines.append(f"Build time: {data.duration_seconds:.1f}s")
    lines.append("")
    lines.append("Per source:")
    for s in data.sources:
        icon = _STATUS_ICON[s.overall]
        age = (
            f"{s.inventory_age_hours:.1f}h"
            if s.inventory_age_hours is not None
            else "n/a"
        )
        delta_str = "n/a"
        if s.count_delta and s.count_delta.get("available"):
            d = s.count_delta["delta"]
            pct = s.count_delta.get("delta_pct")
            if pct is not None:
                delta_str = f"{d:+,} ({pct:+.1f}%)"
            else:
                delta_str = f"{d:+,}"
        rt = "—"
        if s.restore_test:
            rt = s.restore_test.overall
        lines.append(
            f"  {icon} {s.alias:<10}  inventory_age={age:<8}  delta={delta_str:<20}  restore={rt}"
        )
        for note in s.notes:
            lines.append(f"        ↳ {note}")
        if s.pitr_tables:
            failing = [t for t, v in s.pitr_tables.items() if not v.get("enabled")]
            if failing:
                lines.append(f"        ↳ DynamoDB PITR disabled: {', '.join(failing)}")
    lines.append("")
    lines.append("Configuration:")
    lines.append(f"  Canary (daily): {_CANARY_SOURCE}")
    lines.append(
        f"  Today's rotated source: {_ROTATION_BY_WEEKDAY.get(data.report_date.weekday(), '—')}"
    )
    lines.append(f"  Freshness threshold: {_FRESHNESS_THRESHOLD_HOURS}h")
    lines.append(
        f"  Delta thresholds: {_DELTA_ABS_THRESHOLD:,} absolute or "
        f"{_DELTA_PCT_THRESHOLD}% (whichever crossed first)"
    )
    return "\n".join(lines)


def format_slack(data: HealthReportData) -> list[dict[str, Any]]:
    """Slack Block Kit message body."""
    icon = _STATUS_ICON[data.overall]
    headline = (
        f"{icon} NSHM backup health {data.report_date.isoformat()} — "
        f"{_STATUS_TEXT[data.overall]} ({data.healthy_count}/{len(data.sources)})"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": headline, "emoji": True},
        }
    ]
    for s in data.sources:
        lines: list[str] = []
        line = f"*{_STATUS_ICON[s.overall]} {s.alias}*"
        details: list[str] = []
        if s.inventory_age_hours is not None:
            details.append(f"inventory_age={s.inventory_age_hours:.1f}h")
        else:
            details.append("inventory_age=n/a")
        if s.count_delta and s.count_delta.get("available"):
            d = s.count_delta["delta"]
            pct = s.count_delta.get("delta_pct")
            details.append(
                f"delta={d:+,}" + (f" ({pct:+.1f}%)" if pct is not None else "")
            )
        if s.restore_test:
            details.append(f"restore={s.restore_test.overall}")
        if details:
            line += "   " + "   ".join(details)
        lines.append(line)
        for note in s.notes:
            lines.append(f"   _• {note}_")
        if s.pitr_tables:
            failing = [t for t, v in s.pitr_tables.items() if not v.get("enabled")]
            if failing:
                lines.append(f"   _• DynamoDB PITR disabled: {', '.join(failing)}_")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"_Build {data.duration_seconds:.1f}s • "
                        f"Canary {_CANARY_SOURCE} • "
                        f"Freshness threshold {_FRESHNESS_THRESHOLD_HOURS}h_"
                    ),
                }
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass
class DeliveryResult:
    slack_attempted: bool = False
    slack_ok: bool = False
    slack_error: str | None = None
    sns_attempted: bool = False
    sns_ok: bool = False
    sns_error: str | None = None
    sns_message_id: str | None = None


def send(
    data: HealthReportData,
    notifications_config,
    session: boto3.Session,
    reports_topic_arn: str | None,
) -> DeliveryResult:
    """Deliver the report via Slack + SNS per ``notifications`` config.

    Each channel is independent: a failure in one does not block the
    other. Returns a ``DeliveryResult`` for downstream logging / event
    appending.
    """
    result = DeliveryResult()

    slack_cfg = getattr(notifications_config, "slack", None)
    if slack_cfg and getattr(slack_cfg, "enabled", False):
        result.slack_attempted = True
        try:
            webhook = resolve_webhook_url(session, slack_cfg.webhook_url_secret)
            send_slack(
                webhook,
                format_slack(data),
                text=format_email_subject(data),
            )
            result.slack_ok = True
        except SlackDeliveryError as e:
            result.slack_error = str(e)
            logger.exception("Slack delivery failed")
        except Exception as e:
            result.slack_error = f"unexpected: {e}"
            logger.exception("Slack delivery failed (unexpected)")

    reports_cfg = getattr(notifications_config, "reports", None)
    reports_email_cfg = getattr(reports_cfg, "email", None) if reports_cfg else None
    if (
        reports_email_cfg
        and getattr(reports_email_cfg, "enabled", False)
        and reports_topic_arn
    ):
        result.sns_attempted = True
        try:
            result.sns_message_id = publish_report(
                session,
                reports_topic_arn,
                subject=format_email_subject(data),
                body=format_email_text(data),
            )
            result.sns_ok = True
        except SnsDeliveryError as e:
            result.sns_error = str(e)
            logger.exception("SNS delivery failed")
        except Exception as e:
            result.sns_error = f"unexpected: {e}"
            logger.exception("SNS delivery failed (unexpected)")

    return result
