"""Tests for the daily health-report orchestrator."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from aws_snapshot import health_report as hr
from aws_snapshot.commands.test import BucketRestoreResult, RestoreTestResult

# ---------------------------------------------------------------------------
# _classify_source
# ---------------------------------------------------------------------------


def _make_src(**overrides):
    defaults = dict(
        alias="x",
        status_data={},
        inventory_age_hours=2.0,
        inventory_stale=False,
        count_delta={"available": True, "delta": 0, "delta_pct": 0.0},
        divergence={
            "available": True,
            "source_minus_backup": 0,
            "backup_minus_source": 0,
        },
        backup_missing_count=0,
        backup_orphan_count=0,
        restore_test=None,
        pitr_tables={},
    )
    defaults.update(overrides)
    return hr.SourceHealthData(**defaults)


def test_classify_green_with_all_good_signals():
    s = _make_src()
    assert hr._classify_source(s) == "green"


def test_classify_yellow_when_inventory_stale():
    s = _make_src(inventory_age_hours=48.0, inventory_stale=True)
    assert hr._classify_source(s) == "yellow"


def test_classify_red_when_inventory_missing():
    s = _make_src(inventory_age_hours=None)
    assert hr._classify_source(s) == "red"


def test_classify_red_when_restore_failed():
    rt = RestoreTestResult(source="x", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(source_bucket="s", backup_bucket="b", result="failed", sample_count=0)
    ]
    s = _make_src(restore_test=rt)
    assert hr._classify_source(s) == "red"


def test_classify_red_when_pitr_disabled():
    s = _make_src(pitr_tables={"T1": {"enabled": False}})
    assert hr._classify_source(s) == "red"


def test_classify_red_when_backup_missing_source_keys():
    """ADR-009 class-1: backup missing keys source has → red."""
    s = _make_src(backup_missing_count=3)
    assert hr._classify_source(s) == "red"


def test_classify_green_when_backup_has_orphans_only():
    """ADR-009 class-2: backup orphans never colour the row."""
    s = _make_src(backup_orphan_count=12_431, backup_missing_count=0)
    assert hr._classify_source(s) == "green"


def test_classify_green_when_inventory_disabled_and_no_inventory_data():
    """A source opted out of S3 Inventory must not red on missing inventory.

    With inventory_enabled=False the classifier skips the
    ``inventory_age_hours is None → red`` branch. Restore test and PITR
    are the only red signals that still apply.
    """
    s = _make_src(
        inventory_age_hours=None,
        inventory_stale=False,
        count_delta=None,
        divergence=None,
        backup_missing_count=None,
        backup_orphan_count=None,
        inventory_enabled=False,
    )
    assert hr._classify_source(s) == "green"


def test_classify_red_when_inventory_disabled_but_restore_failed():
    """Restore-test failure still reds an opted-out source."""
    rt = RestoreTestResult(source="x", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(source_bucket="s", backup_bucket="b", result="failed", sample_count=0)
    ]
    s = _make_src(
        inventory_age_hours=None,
        inventory_enabled=False,
        restore_test=rt,
    )
    assert hr._classify_source(s) == "red"


# ---------------------------------------------------------------------------
# Process-signal classification (pre-inventory health)
# ---------------------------------------------------------------------------


def _proc(**overrides) -> hr.ProcessSignals:
    return hr.ProcessSignals(**overrides)


def test_classify_red_when_last_backup_older_than_red_threshold():
    """A backup older than _BACKUP_AGE_RED_HOURS reds the source regardless
    of inventory state. Process signal — the schedule stopped firing."""
    s = _make_src(process=_proc(last_backup_age_hours=hr._BACKUP_AGE_RED_HOURS + 1))
    assert hr._classify_source(s) == "red"


def test_classify_yellow_when_last_backup_in_yellow_lane():
    """Backups between yellow and red age thresholds surface yellow."""
    s = _make_src(process=_proc(last_backup_age_hours=hr._BACKUP_AGE_YELLOW_HOURS + 1))
    assert hr._classify_source(s) == "yellow"


def test_classify_green_when_last_backup_fresh():
    s = _make_src(process=_proc(last_backup_age_hours=1.0))
    assert hr._classify_source(s) == "green"


def test_classify_red_when_s3_batch_failure_rate_exceeds_threshold():
    """A recent batch job with >10% failures reds the source."""
    s = _make_src(
        process=_proc(
            last_backup_age_hours=2.0,
            last_s3_batch_jobs=[
                {
                    "source_bucket": "src",
                    "total": 100,
                    "succeeded": 80,
                    "failed": 20,
                    "failure_pct": 0.20,
                }
            ],
        ),
    )
    assert hr._classify_source(s) == "red"


def test_classify_green_when_batch_failure_rate_under_threshold():
    """A handful of failures in a large batch stays green."""
    s = _make_src(
        process=_proc(
            last_backup_age_hours=2.0,
            last_s3_batch_jobs=[
                {
                    "source_bucket": "src",
                    "total": 1_000,
                    "succeeded": 998,
                    "failed": 2,
                    "failure_pct": 0.002,
                }
            ],
        ),
    )
    assert hr._classify_source(s) == "green"


def test_classify_red_when_ddb_export_failed():
    s = _make_src(
        process=_proc(
            last_backup_age_hours=2.0,
            ddb_export_summary={"completed": 5, "failed": 1, "in_progress": 0, "no_recent": 0},
        )
    )
    assert hr._classify_source(s) == "red"


def test_classify_green_when_inventory_disabled_and_process_healthy():
    """The user's intended steady-state: opt out of inventory, rely on
    process signals + restore test + PITR. Source classifies GREEN."""
    s = _make_src(
        inventory_age_hours=None,
        inventory_stale=False,
        count_delta=None,
        divergence=None,
        backup_missing_count=None,
        backup_orphan_count=None,
        inventory_enabled=False,
        process=_proc(
            last_backup_age_hours=2.0,
            ddb_export_summary={"completed": 5, "failed": 0, "in_progress": 0, "no_recent": 0},
            last_s3_batch_jobs=[
                {
                    "source_bucket": "src",
                    "total": 1000,
                    "succeeded": 1000,
                    "failed": 0,
                    "failure_pct": 0.0,
                }
            ],
        ),
    )
    assert hr._classify_source(s) == "green"


# ---------------------------------------------------------------------------
# _extract_process_signals
# ---------------------------------------------------------------------------


def test_extract_empty_status_returns_defaults():
    now = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    sig = hr._extract_process_signals({}, now)
    assert sig.last_backup_at is None
    assert sig.last_backup_age_hours is None
    assert sig.last_s3_batch_jobs == []
    assert sig.ddb_export_summary == {
        "completed": 0,
        "in_progress": 0,
        "failed": 0,
        "no_recent": 0,
        "errored": 0,
    }


def test_extract_aggregates_last_backup_across_buckets():
    """Fields read from run-state must match the run_state.py write schema.

    Specifically: ``status`` is the run phase ("completed"/"skipped"
    are the success values), ``checked_at`` is the only timestamp.
    See https://github.com/GNS-Science/nzshm-backup#63 regression that
    used wrong field names.
    """
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    status = {
        "s3_batches": [
            {
                "source_bucket": "a",
                "backup_bucket": "ba",
                "last_run": {
                    "status": "completed",
                    "checked_at": "2026-06-27T10:00:00+00:00",
                },
                "recent_jobs": [
                    {
                        "job_id": "j1",
                        "status": "Complete",
                        "creation_time": "2026-06-27T10:00:00+00:00",
                        "total": 10,
                        "succeeded": 10,
                        "failed": 0,
                    }
                ],
            },
            {
                "source_bucket": "b",
                "backup_bucket": "bb",
                # b is older — should not become the aggregate "last backup"
                "last_run": {
                    "status": "skipped",
                    "checked_at": "2026-06-27T05:00:00+00:00",
                },
                "recent_jobs": [],
            },
        ]
    }
    sig = hr._extract_process_signals(status, now)
    assert sig.last_backup_at == datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)
    assert sig.last_backup_age_hours == 2.0
    assert len(sig.last_s3_batch_jobs) == 1  # only bucket a had a recent_jobs entry
    assert sig.last_s3_batch_jobs[0]["job_id"] == "j1"
    assert sig.last_s3_batch_jobs[0]["creation_time"] == "2026-06-27T10:00:00+00:00"


def test_extract_ignores_unfinished_or_failed_run_state():
    """Only ``completed`` and ``skipped`` runs count as "last backup".

    Other statuses (``running``, ``prepared``, ``submitted``, ``failed``)
    represent in-flight or failed attempts and must not silently advance
    the last-backup timestamp.
    """
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    status = {
        "s3_batches": [
            {
                "source_bucket": "a",
                "backup_bucket": "ba",
                "last_run": {
                    "status": "failed",
                    "checked_at": "2026-06-27T11:00:00+00:00",
                },
                "recent_jobs": [],
            },
            {
                "source_bucket": "b",
                "backup_bucket": "bb",
                "last_run": {
                    "status": "running",
                    "checked_at": "2026-06-27T11:30:00+00:00",
                },
                "recent_jobs": [],
            },
        ]
    }
    sig = hr._extract_process_signals(status, now)
    assert sig.last_backup_at is None
    assert sig.last_backup_age_hours is None


def test_extract_summarises_ddb_exports_by_status():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    status = {
        "dynamodb_tables": {
            "ok-1": [{"status": "COMPLETED", "export_time": "2026-06-27T11:00:00+00:00"}],
            "ok-2": [{"status": "COMPLETED", "export_time": "2026-06-27T11:00:00+00:00"}],
            "running": [{"status": "IN_PROGRESS", "export_time": ""}],
            "broken": [{"status": "FAILED", "export_time": ""}],
            "never": [],
            "errored": {"error": "DescribeContinuousBackups: AccessDenied"},
        }
    }
    sig = hr._extract_process_signals(status, now)
    assert sig.ddb_export_summary == {
        "completed": 2,
        "in_progress": 1,
        "failed": 1,
        "no_recent": 1,
        "errored": 1,
    }


def test_extract_uses_ddb_export_ts_when_no_s3_batches():
    """A DDB-only source (or s3 not yet run) gets last_backup_at from DDB."""
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    status = {
        "dynamodb_tables": {
            "t": [{"status": "COMPLETED", "export_time": "2026-06-27T11:30:00+00:00"}],
        }
    }
    sig = hr._extract_process_signals(status, now)
    assert sig.last_backup_at == datetime(2026, 6, 27, 11, 30, tzinfo=timezone.utc)
    assert sig.last_backup_age_hours == 0.5


def test_classify_green_despite_large_source_count_change():
    """ADR-009 reclassifies source-count delta as informational only.

    A large day-over-day drop (previously class-1 red) must not flip the
    row to red on its own.
    """
    s = _make_src(count_delta={"available": True, "delta": -20_000, "delta_pct": -2.0})
    assert hr._classify_source(s) == "green"


# ---------------------------------------------------------------------------
# HealthReportData.overall aggregation
# ---------------------------------------------------------------------------


def test_overall_green_when_all_green():
    data = hr.HealthReportData(report_date=date(2026, 5, 20))
    data.sources = [_make_src(alias="a"), _make_src(alias="b")]
    for s in data.sources:
        s.overall = "green"
    assert data.overall == "green"
    assert data.healthy_count == 2


def test_overall_yellow_when_any_yellow():
    data = hr.HealthReportData(report_date=date(2026, 5, 20))
    data.sources = [_make_src(alias="a"), _make_src(alias="b")]
    data.sources[0].overall = "green"
    data.sources[1].overall = "yellow"
    assert data.overall == "yellow"


def test_overall_red_dominates_yellow():
    data = hr.HealthReportData(report_date=date(2026, 5, 20))
    data.sources = [_make_src(alias="a"), _make_src(alias="b")]
    data.sources[0].overall = "yellow"
    data.sources[1].overall = "red"
    assert data.overall == "red"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _make_report_green() -> hr.HealthReportData:
    data = hr.HealthReportData(report_date=date(2026, 5, 20))
    data.duration_seconds = 12.4
    data.sources = [
        _make_src(
            alias="toshi",
            inventory_age_hours=6.5,
            count_delta={"available": True, "delta": 5, "delta_pct": 0.001},
        ),
        _make_src(
            alias="weka",
            inventory_age_hours=6.6,
            restore_test=RestoreTestResult(
                source="weka",
                mode="direct copy",
                buckets=[
                    BucketRestoreResult(
                        source_bucket="s",
                        backup_bucket="b",
                        result="passed",
                        sample_count=10,
                    )
                ],
            ),
        ),
    ]
    for s in data.sources:
        s.overall = "green"
    return data


def test_format_email_subject_green():
    data = _make_report_green()
    subject = hr.format_email_subject(data)
    assert "2026-05-20" in subject
    assert "GREEN" in subject
    assert "(2/2)" in subject


def test_format_email_subject_red():
    data = _make_report_green()
    data.sources[0].overall = "red"
    subject = hr.format_email_subject(data)
    assert "RED" in subject
    assert "(1/2)" in subject


def test_format_email_text_includes_all_sources_and_overall():
    data = _make_report_green()
    body = hr.format_email_text(data)
    assert "NSHM Backup Health Report" in body
    assert "2026-05-20" in body
    assert "Overall: GREEN" in body
    assert "toshi" in body
    assert "weka" in body
    assert "restore=passed" in body
    # canary documented
    assert "Canary (daily): weka" in body


def test_format_email_text_handles_no_inventory_data():
    data = _make_report_green()
    data.sources[0].inventory_age_hours = None
    data.sources[0].notes = ["no inventory data available"]
    body = hr.format_email_text(data)
    assert "inventory_age=n/a" in body
    assert "no inventory data available" in body


def test_format_slack_returns_blocks_with_header_and_sections():
    data = _make_report_green()
    blocks = hr.format_slack(data)
    assert blocks[0]["type"] == "header"
    assert "GREEN" in blocks[0]["text"]["text"]
    section_texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    # one section per source
    assert any("toshi" in t for t in section_texts)
    assert any("weka" in t for t in section_texts)


# ---------------------------------------------------------------------------
# send() — delivery routing
# ---------------------------------------------------------------------------


def test_send_skips_both_when_disabled():
    data = _make_report_green()
    config = MagicMock()
    config.slack = MagicMock(enabled=False)
    config.reports = MagicMock()
    config.reports.email = MagicMock(enabled=False)
    session = MagicMock()

    result = hr.send(data, config, session, "arn:aws:sns:::topic")

    assert result.slack_attempted is False
    assert result.sns_attempted is False


def test_send_slack_only_when_only_slack_enabled():
    data = _make_report_green()
    config = MagicMock()
    config.slack = MagicMock(enabled=True, webhook_url_secret="backup-slack-webhook")
    config.reports = MagicMock()
    config.reports.email = MagicMock(enabled=False)
    session = MagicMock()

    with patch.object(hr, "resolve_webhook_url", return_value="https://hooks/x"):
        with patch.object(hr, "send_slack") as mock_send:
            result = hr.send(data, config, session, "arn:aws:sns:::topic")

    assert result.slack_attempted is True
    assert result.slack_ok is True
    assert result.sns_attempted is False
    mock_send.assert_called_once()


def test_send_continues_to_sns_when_slack_fails():
    data = _make_report_green()
    config = MagicMock()
    config.slack = MagicMock(enabled=True, webhook_url_secret="backup-slack-webhook")
    config.reports = MagicMock()
    config.reports.email = MagicMock(enabled=True, address="me@example.com")
    session = MagicMock()

    with patch.object(hr, "resolve_webhook_url", return_value="https://hooks/x"):
        with patch.object(hr, "send_slack", side_effect=hr.SlackDeliveryError("HTTP 500")):
            with patch.object(hr, "publish_report", return_value="msg-1"):
                result = hr.send(data, config, session, "arn:aws:sns:::topic")

    assert result.slack_attempted is True
    assert result.slack_ok is False
    assert "HTTP 500" in result.slack_error
    assert result.sns_attempted is True
    assert result.sns_ok is True
    assert result.sns_message_id == "msg-1"


def test_send_sns_skipped_when_no_topic_arn():
    data = _make_report_green()
    config = MagicMock()
    config.slack = MagicMock(enabled=False)
    config.reports = MagicMock()
    config.reports.email = MagicMock(enabled=True, address="me@example.com")
    session = MagicMock()

    result = hr.send(data, config, session, reports_topic_arn=None)

    assert result.sns_attempted is False


# ---------------------------------------------------------------------------
# build_report — integration with mocked reused functions
# ---------------------------------------------------------------------------


_DEFAULT_DIVERGENCE = {
    "available": True,
    "source_minus_backup": 0,
    "backup_minus_source": 0,
    "source_dt": "2026-05-25-00-00",
    "backup_dt": "2026-05-25-00-00",
}


def _build_report_mocks():
    """Common config + mock return values for build_report integration tests."""
    config = MagicMock()
    bucket_cfg = MagicMock()
    bucket_cfg.arn = "arn:aws:s3:::src-bucket"
    bucket_cfg.label = "main"
    source_cfg = MagicMock()
    source_cfg.s3_buckets = [bucket_cfg]
    source_cfg.dynamodb_tables = []
    source_cfg.source_account_id = "999"
    source_cfg.source_account_role_arn = None
    source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
    source_cfg.inventory_enabled = True
    config.general.region = "ap-southeast-2"
    config.notifications.reports.health = None  # use module-default thresholds
    return config, source_cfg


@patch("aws_snapshot.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_assembles_sources_and_runs_canary_only_on_off_day(
    _mock_account, mock_status, mock_inv, mock_delta, mock_restore, _mock_div
):
    """On Tuesday (weekday=1), only weka (canary) gets a restore test."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"weka": source_cfg, "toshi": source_cfg}

    mock_status.return_value = {"weka": {}, "toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
        "today_count": 100,
        "yesterday_count": 100,
    }

    rt = RestoreTestResult(source="weka", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(source_bucket="s", backup_bucket="b", result="passed", sample_count=10)
    ]
    mock_restore.return_value = rt

    session = MagicMock()
    # Tuesday — no rotation entry; only canary
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    assert len(report.sources) == 2
    # restore called once (weka only); toshi not in rotation Tuesday
    assert mock_restore.call_count == 1
    called_alias = mock_restore.call_args.kwargs["source_alias"]
    assert called_alias == "weka"


@patch("aws_snapshot.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_runs_canary_plus_rotated_source_on_rotation_day(
    _mock_account, mock_status, mock_inv, mock_delta, mock_restore, _mock_div
):
    """On Monday (weekday=0), weka + ths both get restore-tested."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"weka": source_cfg, "ths": source_cfg, "toshi": source_cfg}

    mock_status.return_value = {"weka": {}, "ths": {}, "toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
    }

    rt = RestoreTestResult(source="x", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(source_bucket="s", backup_bucket="b", result="passed", sample_count=10)
    ]
    mock_restore.return_value = rt

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 18), weekday=0)

    assert len(report.sources) == 3
    # restore called twice: weka + ths (Monday rotation)
    assert mock_restore.call_count == 2
    aliases = {c.kwargs["source_alias"] for c in mock_restore.call_args_list}
    assert aliases == {"weka", "ths"}


@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_classifies_red_on_backup_missing(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, mock_div
):
    """ADR-009 class-1: source has keys backup doesn't → red + warning note."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
    }
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 3,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.backup_missing_count == 3
    assert src.overall == "red"
    assert any("missing 3 source keys" in n for n in src.notes)


@patch("aws_snapshot.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_source_count_drop_is_class2_informational_only(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, _mock_div
):
    """ADR-009 reclassification: a large source-count drop is info-only, not red."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {
        "available": True,
        "delta": -50_000,
        "delta_pct": -8.0,
        "today_count": 100_000,
        "yesterday_count": 150_000,
    }

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "green"  # not red — count delta no longer alarms
    assert any("dropped" in n for n in src.info_notes)
    assert not any("dropped" in n for n in src.notes)


@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_orphan_count_is_class2_info(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, mock_div
):
    """ADR-009 class-2: backup orphans appear as info, don't colour the row."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
    }
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 0,
        "backup_minus_source": 12_431,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.backup_orphan_count == 12_431
    assert src.overall == "green"
    assert any("orphans" in n for n in src.info_notes)


# ---------------------------------------------------------------------------
# Head-check tag on class-1 RED (still missing / auto-healed / mixed)
# ---------------------------------------------------------------------------


def _client_error(code: str) -> ClientError:
    """Construct a botocore.ClientError with the given S3 error code."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


def _session_with_head_object_results(results: list[object]) -> MagicMock:
    """Build a session whose s3 client's head_object iterates through results.

    Each entry is either an exception (raised on that call) or a dict
    (returned on that call). Useful for mixed still-missing/auto-healed
    scenarios.
    """
    session = MagicMock()
    s3 = MagicMock()

    iter_results = iter(results)

    def _head(**kwargs):
        item = next(iter_results)
        if isinstance(item, Exception):
            raise item
        return item

    s3.head_object.side_effect = _head
    session.client.return_value = s3
    return session


@patch("aws_snapshot.health_report.divergence_sample_keys")
@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_tags_still_missing_when_head_object_404s(
    _mock_account,
    mock_status,
    mock_inv,
    mock_delta,
    _mock_restore,
    mock_div,
    mock_sample,
):
    """Sampled keys all 404 → tag '(still missing live, sampled N)'.

    Scenario A from the head-check validation: backup-side delete has
    not yet been re-synced; live state still shows the gap.
    """
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}
    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {"available": True, "delta": 0, "delta_pct": 0.0}
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 1,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }
    mock_sample.return_value = {
        "available": True,
        "source_minus_backup_sample": ["data/file-01.txt"],
        "sample_size": 1,
    }

    session = _session_with_head_object_results([_client_error("404")])
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "red"
    assert any("missing 1 source keys (still missing live, sampled 1)" in n for n in src.notes), (
        src.notes
    )


@patch("aws_snapshot.health_report.divergence_sample_keys")
@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_tags_auto_healed_when_head_object_200s(
    _mock_account,
    mock_status,
    mock_inv,
    mock_delta,
    _mock_restore,
    mock_div,
    mock_sample,
):
    """Sampled keys all 200 → tag '(auto-healed since snapshot, sampled N)'.

    Scenario AA from the head-check validation: backup has re-synced
    between the snapshot and the report; the gap exists on disk no
    more, but the audit signal still fires RED (decision: keep RED).
    """
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}
    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {"available": True, "delta": 0, "delta_pct": 0.0}
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 1,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }
    mock_sample.return_value = {
        "available": True,
        "source_minus_backup_sample": ["data/file-01.txt"],
        "sample_size": 1,
    }

    session = _session_with_head_object_results([{"ContentLength": 71}])
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "red"  # decision: keep RED regardless of live state
    assert any(
        "missing 1 source keys (auto-healed since snapshot, sampled 1)" in n for n in src.notes
    ), src.notes


@patch("aws_snapshot.health_report.divergence_sample_keys")
@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_tags_mixed_when_some_still_missing_some_healed(
    _mock_account,
    mock_status,
    mock_inv,
    mock_delta,
    _mock_restore,
    mock_div,
    mock_sample,
):
    """Partial recovery → tag '(X still missing, Y auto-healed, sampled N)'."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}
    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {"available": True, "delta": 0, "delta_pct": 0.0}
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 4,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }
    mock_sample.return_value = {
        "available": True,
        "source_minus_backup_sample": [
            "data/file-01.txt",
            "data/file-02.txt",
            "data/file-03.txt",
            "data/file-04.txt",
        ],
        "sample_size": 4,
    }

    # 1 still missing, 3 auto-healed
    session = _session_with_head_object_results(
        [
            _client_error("404"),
            {"ContentLength": 71},
            {"ContentLength": 72},
            {"ContentLength": 73},
        ]
    )
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "red"
    assert any(
        "missing 4 source keys (1 still missing, 3 auto-healed, sampled 4)" in n for n in src.notes
    ), src.notes


@patch("aws_snapshot.health_report.divergence_sample_keys")
@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_falls_back_to_untagged_when_sample_query_fails(
    _mock_account,
    mock_status,
    mock_inv,
    mock_delta,
    _mock_restore,
    mock_div,
    mock_sample,
):
    """A failed sample query falls back to the original untagged note shape.

    The class-1 RED still fires (it's based on the count, not the sample);
    only the live-state tag is missing. A separate diagnostic note records
    why the sample failed.
    """
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}
    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {"available": True, "delta": 0, "delta_pct": 0.0}
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 3,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }
    mock_sample.side_effect = RuntimeError("athena workgroup unavailable")

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "red"
    # Untagged note shape — no '(...)' suffix
    assert any(n == "backup is missing 3 source keys" for n in src.notes), src.notes
    # Diagnostic note recording the sample failure
    assert any("head-check sample failed" in n for n in src.notes), src.notes


@patch("aws_snapshot.health_report.divergence_sample_keys")
@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_classifier_unchanged_when_all_auto_healed(
    _mock_account,
    mock_status,
    mock_inv,
    mock_delta,
    _mock_restore,
    mock_div,
    mock_sample,
):
    """Decision: row stays RED even when all sampled keys are auto-healed.

    Audit framing per ADR-009 — the gap existed at snapshot time and
    deserves operator attention regardless of self-heal.
    """
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}
    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {"effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)}
    mock_delta.return_value = {"available": True, "delta": 0, "delta_pct": 0.0}
    mock_div.return_value = {
        "available": True,
        "source_minus_backup": 5,
        "backup_minus_source": 0,
        "source_dt": "2026-05-25-00-00",
        "backup_dt": "2026-05-25-00-00",
    }
    mock_sample.return_value = {
        "available": True,
        "source_minus_backup_sample": [
            "data/file-01.txt",
            "data/file-02.txt",
            "data/file-03.txt",
            "data/file-04.txt",
            "data/file-05.txt",
        ],
        "sample_size": 5,
    }

    # All 5 succeed → auto_healed = 5, still_missing = 0
    session = _session_with_head_object_results(
        [
            {"ContentLength": 71},
            {"ContentLength": 72},
            {"ContentLength": 73},
            {"ContentLength": 74},
            {"ContentLength": 75},
        ]
    )
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    src = report.sources[0]
    assert src.overall == "red", (
        "Decision: keep RED even when all sampled keys are auto-healed "
        "— audit framing trumps live state."
    )


@patch("aws_snapshot.health_report.divergence_counts")
@patch("aws_snapshot.health_report.restore_test_source")
@patch("aws_snapshot.health_report.count_delta")
@patch("aws_snapshot.health_report.inventory_health_for_bucket_pair")
@patch("aws_snapshot.health_report.get_status_dict")
@patch("aws_snapshot.health_report.get_account_id", return_value="999")
def test_build_report_skips_athena_when_inventory_disabled(
    _mock_account, mock_status, mock_inv, mock_delta, mock_restore, mock_div
):
    """A source with inventory_enabled=False must not call the Athena helpers
    and must surface a class-2 info_note instead of a red row."""
    config, source_cfg = _build_report_mocks()
    source_cfg.inventory_enabled = False
    config.sources = {"toy-noinv": source_cfg}
    mock_status.return_value = {"toy-noinv": {}}
    mock_restore.return_value = None  # not on rotation today

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 19), weekday=1)

    # Athena-backed helpers never invoked
    assert mock_inv.call_count == 0
    assert mock_delta.call_count == 0
    assert mock_div.call_count == 0

    src = report.sources[0]
    assert src.inventory_enabled is False
    assert src.inventory_age_hours is None
    assert src.divergence is None
    assert src.count_delta is None
    assert src.overall == "green"  # no inventory ≠ red when opt-in is off
    # Inventory-disabled sources surface NO inventory-related notes — the
    # absence of inventory chips elsewhere is the signal. A daily
    # "inventory disabled" info_note is daily noise without information.
    assert not any("inventory" in n.lower() for n in src.info_notes)
    assert not any("inventory" in n.lower() for n in src.notes)


# ---------------------------------------------------------------------------
# CLI: _resolve_reports_topic_arn
# ---------------------------------------------------------------------------


def test_resolve_topic_arn_prefers_explicit_flag(monkeypatch):
    from aws_snapshot.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.setenv("BACKUP_REPORTS_TOPIC_ARN", "arn:from-env")
    session = MagicMock()
    arn = _resolve_reports_topic_arn("arn:from-flag", session, "prod")
    assert arn == "arn:from-flag"


def test_resolve_topic_arn_falls_back_to_env(monkeypatch):
    from aws_snapshot.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.setenv("BACKUP_REPORTS_TOPIC_ARN", "arn:from-env")
    session = MagicMock()
    arn = _resolve_reports_topic_arn(None, session, "prod")
    assert arn == "arn:from-env"


def test_resolve_topic_arn_constructs_from_session_when_no_overrides(monkeypatch):
    from aws_snapshot.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.delenv("BACKUP_REPORTS_TOPIC_ARN", raising=False)
    session = MagicMock()
    session.region_name = "ap-southeast-2"
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts

    arn = _resolve_reports_topic_arn(None, session, "prod")
    assert arn == "arn:aws:sns:ap-southeast-2:123456789012:nzshm-backup-reports-prod"


def test_resolve_topic_arn_uses_stage_in_constructed_name(monkeypatch):
    from aws_snapshot.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.delenv("BACKUP_REPORTS_TOPIC_ARN", raising=False)
    session = MagicMock()
    session.region_name = "ap-southeast-2"
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    session.client.return_value = sts

    arn = _resolve_reports_topic_arn(None, session, "sandbox")
    assert arn.endswith("nzshm-backup-reports-sandbox")
