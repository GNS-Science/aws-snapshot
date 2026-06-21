"""Daily health-report orchestrator (ADR-005 slow path, ADR-009 signal model).

Per-source signal classes (see ADR-009):

* **Class 1 — backup-system correctness** (red):
  restore-test failure, DynamoDB PITR disabled, inventory missing entirely,
  or backup-missing-source-keys (``divergence_counts.source_minus_backup``).
* **Class 2 — operational news** (informational, never red):
  source-count change vs yesterday, backup orphan accumulation
  (``divergence_counts.backup_minus_source``).
* **Class 3 — forward-looking risk** (yellow):
  inventory freshness > 30h.

Inputs:

- backup state from ``commands.status.get_status_dict``
- inventory freshness from ``inventory_state.inventory_health_for_bucket_pair``
- source-vs-backup divergence from ``athena_inventory.divergence_counts``
  (one Athena scan returns both directions per ADR-009)
- day-over-day source count from ``athena_inventory.count_delta``
  (informational only after ADR-009)
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
from botocore.exceptions import ClientError

from nzshm_backup.athena_inventory import (
    count_delta,
    divergence_counts,
    divergence_sample_keys,
)
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
# Default thresholds — also live as defaults on HealthReportConfig in
# config/models.py. The orchestrator reads from config.notifications.reports
# .health which falls back to these via Pydantic's default_factory.
# ---------------------------------------------------------------------------

_CANARY_SOURCE = "weka"
_ROTATION_BY_WEEKDAY: dict[int, str] = {
    0: "ths",  # Monday
    2: "toshi",  # Wednesday
    4: "static",  # Friday
}
_FRESHNESS_THRESHOLD_HOURS = 30.0  # ADR-007 mit. 4
_RESTORE_SAMPLE_SIZE = 10

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
    divergence: dict[str, Any] | None  # None if not computed (e.g. no inventory)
    # Class-1 alert: backup is missing source keys (system has actually failed).
    backup_missing_count: int | None  # None if divergence unavailable
    # Class-2 info: backup carries keys source no longer has.
    backup_orphan_count: int | None
    restore_test: RestoreTestResult | None  # None if not tested this run
    pitr_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When False, the source has opted out of S3 Inventory (see
    # ``SourceConfig.inventory_enabled``). The classifier must not red on
    # missing inventory data — restore test becomes the dominant red signal.
    inventory_enabled: bool = True
    overall: Status = "green"
    # Operational errors and class-1/3 detail strings (warning glyph).
    notes: list[str] = field(default_factory=list)
    # Class-2 informational lines (info glyph; never colours the row).
    info_notes: list[str] = field(default_factory=list)


@dataclass
class HealthReportData:
    report_date: date
    sources: list[SourceHealthData] = field(default_factory=list)
    duration_seconds: float = 0.0
    # Snapshot of the tunables actually used during this build, so the
    # footer reflects config overrides rather than module defaults.
    canary_source: str = "weka"
    rotation_by_weekday: dict[int, str] = field(default_factory=dict)
    freshness_threshold_hours: float = 30.0

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
    """Map signals → green / yellow / red per ADR-009.

    Class 1 → red (any of):
      - restore-test failure
      - PITR disabled on any configured DynamoDB table
      - inventory missing entirely (only when ``inventory_enabled`` is True
        for the source; opted-out sources don't red on missing inventory)
      - backup is missing keys that source has
        (``backup_missing_count`` > 0)

    Class 3 → yellow (and no class 1):
      - inventory present but stale (> threshold)

    Class 2 signals (source-count delta, backup orphan accumulation) never
    affect colour; they appear in the report body via ``info_notes``.
    """
    if s.restore_test and s.restore_test.overall == "failed":
        return "red"
    if any(not t.get("enabled") for t in s.pitr_tables.values()):
        return "red"
    if s.inventory_enabled and s.inventory_age_hours is None:
        return "red"
    if s.backup_missing_count and s.backup_missing_count > 0:
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

    # Read tunables from config when present; fall back to module defaults
    # so older configs (no health: block) keep working unchanged.
    h_cfg = getattr(getattr(config.notifications, "reports", None), "health", None)
    canary = getattr(h_cfg, "canary_source", _CANARY_SOURCE)
    rotation_map = getattr(h_cfg, "rotation_by_weekday", _ROTATION_BY_WEEKDAY)
    freshness_threshold_hours = getattr(
        h_cfg, "freshness_threshold_hours", _FRESHNESS_THRESHOLD_HOURS
    )
    restore_sample_size = getattr(h_cfg, "restore_sample_size", _RESTORE_SAMPLE_SIZE)

    aliases = list(config.sources.keys())
    status_data = get_status_dict(aliases, config, session)
    account_id = get_account_id(session)

    rotated = rotation_map.get(weekday)
    sources_to_restore_test = {canary}
    if rotated and rotated in config.sources:
        sources_to_restore_test.add(rotated)

    report = HealthReportData(
        report_date=today,
        canary_source=canary,
        rotation_by_weekday=rotation_map,
        freshness_threshold_hours=freshness_threshold_hours,
    )
    now_utc = datetime.now(timezone.utc)

    for alias in aliases:
        source_config = config.sources[alias]
        source_account_id = source_config.source_account_id or account_id
        notes: list[str] = []
        info_notes: list[str] = []

        source_bucket: str | None = None
        backup_bucket: str | None = None
        if source_config.s3_buckets:
            bucket_cfg = source_config.s3_buckets[0]
            source_bucket = bucket_cfg.arn.split(":::")[-1]
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_cfg.label, config.general.region, source_account_id, alias
            )

        # Inventory-based signals are gated on inventory_enabled. When opted
        # out, the source surfaces no inventory-age, divergence, or
        # count-delta lines — restore test (and PITR) become dominant.
        inv_age: float | None = None
        inv_stale = False
        delta: dict[str, Any] | None = None
        divergence: dict[str, Any] | None = None
        backup_missing: int | None = None
        backup_orphans: int | None = None

        if source_config.inventory_enabled:
            # Inventory freshness (class 3 — yellow when present-but-stale).
            if source_bucket and backup_bucket:
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
                        inv_stale = inv_age > freshness_threshold_hours
                    else:
                        notes.append("no inventory data available")
                except Exception as e:
                    notes.append(f"inventory health check failed: {e}")

            # Day-over-day source count (class 2 — informational only).
            if source_bucket:
                try:
                    delta = count_delta(session, alias, "source", source_bucket)
                    if delta.get("available"):
                        d = delta.get("delta") or 0
                        pct = delta.get("delta_pct")
                        if d != 0:
                            pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
                            verb = "grew" if d > 0 else "dropped"
                            info_notes.append(
                                f"source {verb} by {abs(d):,} objects vs yesterday{pct_str}"
                            )
                except Exception as e:
                    notes.append(f"count delta check failed: {e}")

            # Source-vs-backup divergence (class 1 backup-missing + class 2 orphans).
            if source_bucket and backup_bucket:
                try:
                    divergence = divergence_counts(session, alias, source_bucket, backup_bucket)
                    if divergence.get("available"):
                        backup_missing = divergence["source_minus_backup"]
                        backup_orphans = divergence["backup_minus_source"]
                        if backup_missing and backup_missing > 0:
                            # Sample up to 10 missing keys and head_object each
                            # against the live backup bucket. Distinguishes a
                            # *current* gap from one already self-healed by a
                            # subsequent backup run. Note that the row stays
                            # RED either way — the tag is for operator
                            # clarity, not classification (audit framing per
                            # ADR-009).
                            still_missing = 0
                            auto_healed = 0
                            sample_size = 0
                            try:
                                sample = divergence_sample_keys(
                                    session,
                                    alias,
                                    source_bucket,
                                    backup_bucket,
                                    limit=10,
                                )
                                if sample.get("available"):
                                    backup_s3 = session.client("s3")
                                    for k in sample.get("source_minus_backup_sample", []):
                                        sample_size += 1
                                        try:
                                            backup_s3.head_object(Bucket=backup_bucket, Key=k)
                                            auto_healed += 1
                                        except ClientError as e:
                                            code = e.response.get("Error", {}).get("Code", "")
                                            if code in ("404", "NoSuchKey", "NotFound"):
                                                still_missing += 1
                                            else:
                                                raise
                            except Exception as e:
                                notes.append(f"head-check sample failed: {e}")

                            if sample_size == 0:
                                tag = ""
                            elif still_missing == sample_size:
                                tag = f" (still missing live, sampled {sample_size})"
                            elif auto_healed == sample_size:
                                tag = f" (auto-healed since snapshot, sampled {sample_size})"
                            else:
                                tag = (
                                    f" ({still_missing} still missing, "
                                    f"{auto_healed} auto-healed, "
                                    f"sampled {sample_size})"
                                )
                            notes.append(f"backup is missing {backup_missing:,} source keys{tag}")
                        if backup_orphans and backup_orphans > 0:
                            info_notes.append(
                                f"backup has {backup_orphans:,} orphans "
                                "(source-side deletions retained per ADR-006)"
                            )
                except Exception as e:
                    notes.append(f"divergence check failed: {e}")
        else:
            info_notes.append(
                "inventory disabled for this source — restore test is the dominant signal"
            )

        # Restore verification (canary + rotation).
        restore_result: RestoreTestResult | None = None
        if alias in sources_to_restore_test:
            try:
                restore_result = restore_test_source(
                    session=session,
                    config=config,
                    source_alias=alias,
                    sample_size=restore_sample_size,
                    use_batch=False,
                    emit_events=True,
                )
            except Exception as e:
                notes.append(f"restore test exception: {e}")

        pitr = _check_dynamodb_pitr(session, source_config, source_config.source_account_role_arn)

        src = SourceHealthData(
            alias=alias,
            status_data=status_data.get(alias, {}),
            inventory_age_hours=inv_age,
            inventory_stale=inv_stale,
            count_delta=delta,
            divergence=divergence,
            backup_missing_count=backup_missing,
            backup_orphan_count=backup_orphans,
            restore_test=restore_result,
            pitr_tables=pitr,
            inventory_enabled=source_config.inventory_enabled,
            notes=notes,
            info_notes=info_notes,
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
        age = f"{s.inventory_age_hours:.1f}h" if s.inventory_age_hours is not None else "n/a"
        rt = "—"
        if s.restore_test:
            rt = s.restore_test.overall
        lines.append(f"  {icon} {s.alias:<10}  inventory_age={age:<8}  restore={rt}")
        for note in s.notes:
            lines.append(f"        ⚠ {note}")
        if s.pitr_tables:
            failing = [t for t, v in s.pitr_tables.items() if not v.get("enabled")]
            if failing:
                lines.append(f"        ⚠ DynamoDB PITR disabled: {', '.join(failing)}")
        for info in s.info_notes:
            lines.append(f"        ℹ {info}")
    lines.append("")
    lines.append("Configuration:")
    lines.append(f"  Canary (daily): {data.canary_source}")
    lines.append(
        f"  Today's rotated source: {data.rotation_by_weekday.get(data.report_date.weekday(), '—')}"
    )
    lines.append(f"  Freshness threshold: {data.freshness_threshold_hours}h")
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
        if s.restore_test:
            details.append(f"restore={s.restore_test.overall}")
        if details:
            line += "   " + "   ".join(details)
        lines.append(line)
        for note in s.notes:
            lines.append(f"   ⚠ _{note}_")
        if s.pitr_tables:
            failing = [t for t, v in s.pitr_tables.items() if not v.get("enabled")]
            if failing:
                lines.append(f"   ⚠ _DynamoDB PITR disabled: {', '.join(failing)}_")
        for info in s.info_notes:
            lines.append(f"   ℹ _{info}_")
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
                        f"Canary {data.canary_source} • "
                        f"Freshness threshold {data.freshness_threshold_hours}h_"
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
    if reports_email_cfg and getattr(reports_email_cfg, "enabled", False) and reports_topic_arn:
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
