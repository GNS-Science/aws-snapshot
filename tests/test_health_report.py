"""Tests for the daily health-report orchestrator."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from nzshm_backup import health_report as hr
from nzshm_backup.commands.test import BucketRestoreResult, RestoreTestResult

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
        BucketRestoreResult(
            source_bucket="s", backup_bucket="b", result="failed", sample_count=0
        )
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


def test_classify_green_despite_large_source_count_change():
    """ADR-009 reclassifies source-count delta as informational only.

    A large day-over-day drop (previously class-1 red) must not flip the
    row to red on its own.
    """
    s = _make_src(
        count_delta={"available": True, "delta": -20_000, "delta_pct": -2.0}
    )
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
        _make_src(alias="toshi", inventory_age_hours=6.5,
                  count_delta={"available": True, "delta": 5, "delta_pct": 0.001}),
        _make_src(alias="weka", inventory_age_hours=6.6,
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
                  )),
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
    section_texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section"
    ]
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
    config.slack = MagicMock(
        enabled=True, webhook_url_secret="backup-slack-webhook"
    )
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
    config.slack = MagicMock(
        enabled=True, webhook_url_secret="backup-slack-webhook"
    )
    config.reports = MagicMock()
    config.reports.email = MagicMock(enabled=True, address="me@example.com")
    session = MagicMock()

    with patch.object(hr, "resolve_webhook_url", return_value="https://hooks/x"):
        with patch.object(
            hr, "send_slack", side_effect=hr.SlackDeliveryError("HTTP 500")
        ):
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
    config.general.region = "ap-southeast-2"
    config.notifications.reports.health = None  # use module-default thresholds
    return config, source_cfg


@patch("nzshm_backup.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("nzshm_backup.health_report.restore_test_source")
@patch("nzshm_backup.health_report.count_delta")
@patch("nzshm_backup.health_report.inventory_health_for_bucket_pair")
@patch("nzshm_backup.health_report.get_status_dict")
@patch("nzshm_backup.health_report.get_account_id", return_value="999")
def test_build_report_assembles_sources_and_runs_canary_only_on_off_day(
    _mock_account, mock_status, mock_inv, mock_delta, mock_restore, _mock_div
):
    """On Tuesday (weekday=1), only weka (canary) gets a restore test."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"weka": source_cfg, "toshi": source_cfg}

    mock_status.return_value = {"weka": {}, "toshi": {}}
    mock_inv.return_value = {
        "effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)
    }
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
        "today_count": 100,
        "yesterday_count": 100,
    }

    rt = RestoreTestResult(source="weka", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(
            source_bucket="s", backup_bucket="b", result="passed", sample_count=10
        )
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


@patch("nzshm_backup.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("nzshm_backup.health_report.restore_test_source")
@patch("nzshm_backup.health_report.count_delta")
@patch("nzshm_backup.health_report.inventory_health_for_bucket_pair")
@patch("nzshm_backup.health_report.get_status_dict")
@patch("nzshm_backup.health_report.get_account_id", return_value="999")
def test_build_report_runs_canary_plus_rotated_source_on_rotation_day(
    _mock_account, mock_status, mock_inv, mock_delta, mock_restore, _mock_div
):
    """On Monday (weekday=0), weka + ths both get restore-tested."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"weka": source_cfg, "ths": source_cfg, "toshi": source_cfg}

    mock_status.return_value = {"weka": {}, "ths": {}, "toshi": {}}
    mock_inv.return_value = {
        "effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)
    }
    mock_delta.return_value = {
        "available": True,
        "delta": 0,
        "delta_pct": 0.0,
    }

    rt = RestoreTestResult(source="x", mode="direct copy")
    rt.buckets = [
        BucketRestoreResult(
            source_bucket="s", backup_bucket="b", result="passed", sample_count=10
        )
    ]
    mock_restore.return_value = rt

    session = MagicMock()
    report = hr.build_report(session, config, today=date(2026, 5, 18), weekday=0)

    assert len(report.sources) == 3
    # restore called twice: weka + ths (Monday rotation)
    assert mock_restore.call_count == 2
    aliases = {c.kwargs["source_alias"] for c in mock_restore.call_args_list}
    assert aliases == {"weka", "ths"}


@patch("nzshm_backup.health_report.divergence_counts")
@patch("nzshm_backup.health_report.restore_test_source")
@patch("nzshm_backup.health_report.count_delta")
@patch("nzshm_backup.health_report.inventory_health_for_bucket_pair")
@patch("nzshm_backup.health_report.get_status_dict")
@patch("nzshm_backup.health_report.get_account_id", return_value="999")
def test_build_report_classifies_red_on_backup_missing(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, mock_div
):
    """ADR-009 class-1: source has keys backup doesn't → red + warning note."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {
        "effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)
    }
    mock_delta.return_value = {
        "available": True, "delta": 0, "delta_pct": 0.0,
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


@patch("nzshm_backup.health_report.divergence_counts", return_value=_DEFAULT_DIVERGENCE)
@patch("nzshm_backup.health_report.restore_test_source")
@patch("nzshm_backup.health_report.count_delta")
@patch("nzshm_backup.health_report.inventory_health_for_bucket_pair")
@patch("nzshm_backup.health_report.get_status_dict")
@patch("nzshm_backup.health_report.get_account_id", return_value="999")
def test_build_report_source_count_drop_is_class2_informational_only(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, _mock_div
):
    """ADR-009 reclassification: a large source-count drop is info-only, not red."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {
        "effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)
    }
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
    assert src.overall == "green"          # not red — count delta no longer alarms
    assert any("dropped" in n for n in src.info_notes)
    assert not any("dropped" in n for n in src.notes)


@patch("nzshm_backup.health_report.divergence_counts")
@patch("nzshm_backup.health_report.restore_test_source")
@patch("nzshm_backup.health_report.count_delta")
@patch("nzshm_backup.health_report.inventory_health_for_bucket_pair")
@patch("nzshm_backup.health_report.get_status_dict")
@patch("nzshm_backup.health_report.get_account_id", return_value="999")
def test_build_report_orphan_count_is_class2_info(
    _mock_account, mock_status, mock_inv, mock_delta, _mock_restore, mock_div
):
    """ADR-009 class-2: backup orphans appear as info, don't colour the row."""
    config, source_cfg = _build_report_mocks()
    config.sources = {"toshi": source_cfg}

    mock_status.return_value = {"toshi": {}}
    mock_inv.return_value = {
        "effective_data_ts": datetime.now(timezone.utc) - timedelta(hours=2)
    }
    mock_delta.return_value = {
        "available": True, "delta": 0, "delta_pct": 0.0,
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
# CLI: _resolve_reports_topic_arn
# ---------------------------------------------------------------------------


def test_resolve_topic_arn_prefers_explicit_flag(monkeypatch):
    from nzshm_backup.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.setenv("BACKUP_REPORTS_TOPIC_ARN", "arn:from-env")
    session = MagicMock()
    arn = _resolve_reports_topic_arn("arn:from-flag", session, "prod")
    assert arn == "arn:from-flag"


def test_resolve_topic_arn_falls_back_to_env(monkeypatch):
    from nzshm_backup.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.setenv("BACKUP_REPORTS_TOPIC_ARN", "arn:from-env")
    session = MagicMock()
    arn = _resolve_reports_topic_arn(None, session, "prod")
    assert arn == "arn:from-env"


def test_resolve_topic_arn_constructs_from_session_when_no_overrides(monkeypatch):
    from nzshm_backup.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.delenv("BACKUP_REPORTS_TOPIC_ARN", raising=False)
    session = MagicMock()
    session.region_name = "ap-southeast-2"
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts

    arn = _resolve_reports_topic_arn(None, session, "prod")
    assert arn == "arn:aws:sns:ap-southeast-2:123456789012:nzshm-backup-reports-prod"


def test_resolve_topic_arn_uses_stage_in_constructed_name(monkeypatch):
    from nzshm_backup.commands.health_report import _resolve_reports_topic_arn

    monkeypatch.delenv("BACKUP_REPORTS_TOPIC_ARN", raising=False)
    session = MagicMock()
    session.region_name = "ap-southeast-2"
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    session.client.return_value = sts

    arn = _resolve_reports_topic_arn(None, session, "sandbox")
    assert arn.endswith("nzshm-backup-reports-sandbox")
