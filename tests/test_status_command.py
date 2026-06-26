"""Tests for the status command."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from aws_snapshot.cli import app

runner = CliRunner()

REGION = "ap-southeast-2"
ACCOUNT_ID = "123456789012"
TS = datetime(2026, 3, 18, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dynamo_config(tmp_path):
    """Config with one DynamoDB source, no S3."""
    cfg = {
        "general": {"region": REGION},
        "sources": {
            "toshi": {
                "display_name": "ToshiAPI",
                "dynamodb_tables": [f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/TestTable"],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


@pytest.fixture
def s3_incremental_config(tmp_path):
    """Config with one S3 source in incremental (non-batch) mode."""
    cfg = {
        "general": {"region": REGION},
        "sources": {
            "ths": {
                "display_name": "THS",
                "s3_buckets": [{"arn": "arn:aws:s3:::test-ths-bucket", "label": "dataset"}],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


@pytest.fixture
def s3_batch_config(tmp_path):
    """Config with one S3 source in batch mode."""
    cfg = {
        "general": {
            "region": REGION,
            "s3_batch_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:role/nzshm-backup-batch-role",
        },
        "sources": {
            "arkivalist": {
                "display_name": "Arkivalist",
                "use_s3_batch": True,
                "s3_buckets": [{"arn": "arn:aws:s3:::source-bucket", "label": "main"}],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_status_unknown_source_exits_nonzero(dynamo_config, monkeypatch):
    """Unknown --source exits 1 with a helpful error."""
    monkeypatch.chdir(dynamo_config)
    result = runner.invoke(app, ["status", "--source", "nonexistent"])
    assert result.exit_code == 1
    assert "unknown source" in result.output


def test_status_missing_config_exits_nonzero(tmp_path, monkeypatch):
    """No config file → exits 1."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# DynamoDB display
# ---------------------------------------------------------------------------


def test_status_dynamodb_no_exports(dynamo_config, monkeypatch):
    """No exports found → shows 'no exports found' for the table."""
    monkeypatch.chdir(dynamo_config)
    mock_session = MagicMock()
    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[]):
            result = runner.invoke(app, ["status", "--source", "toshi"])

    assert result.exit_code == 0
    assert "no exports found" in result.output
    assert "TestTable" in result.output


def test_status_dynamodb_completed_export(dynamo_config, monkeypatch):
    """Completed export shows ✓ icon and COMPLETED status."""
    monkeypatch.chdir(dynamo_config)

    mock_export = {
        "ExportArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/TestTable/export/abc",
        "ExportStatus": "COMPLETED",
        "ExportTime": TS,
    }
    mock_client = MagicMock()
    mock_client.describe_export.return_value = {
        "ExportDescription": {"StartTime": TS, "FailureMessage": None}
    }
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[mock_export]):
            result = runner.invoke(app, ["status", "--source", "toshi"])

    assert result.exit_code == 0
    assert "✓" in result.output
    assert "COMPLETED" in result.output
    assert "2026-03-18" in result.output


def test_status_dynamodb_failed_export_shows_reason(dynamo_config, monkeypatch):
    """Failed export shows ✗ icon and truncated failure reason."""
    monkeypatch.chdir(dynamo_config)

    mock_export = {
        "ExportArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/TestTable/export/abc",
        "ExportStatus": "FAILED",
        "ExportTime": TS,
    }
    mock_client = MagicMock()
    mock_client.describe_export.return_value = {
        "ExportDescription": {
            "StartTime": TS,
            "FailureMessage": "Export failed because the bucket does not exist",
        }
    }
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[mock_export]):
            result = runner.invoke(app, ["status", "--source", "toshi"])

    assert result.exit_code == 0
    assert "✗" in result.output
    assert "FAILED" in result.output
    assert "bucket does not exist" in result.output


# ---------------------------------------------------------------------------
# S3 incremental display
# ---------------------------------------------------------------------------


def test_status_s3_incremental_with_state(s3_incremental_config, monkeypatch):
    """Incremental mode shows last-run timestamp and status."""
    monkeypatch.chdir(s3_incremental_config)

    mock_state = {
        "checked_at": "2026-03-18T10:00:00",
        "status": "completed",
        "objects_copied": 42,
    }

    with patch("aws_snapshot.commands.status.read_run_state", return_value=mock_state):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status.boto3.Session"):
                result = runner.invoke(app, ["status", "--source", "ths"])

    assert result.exit_code == 0
    assert "2026-03-18" in result.output
    assert "completed" in result.output
    assert "42 objects copied" in result.output


def test_status_s3_incremental_no_state(s3_incremental_config, monkeypatch):
    """Incremental mode with no state file shows nothing for last run."""
    monkeypatch.chdir(s3_incremental_config)

    with patch("aws_snapshot.commands.status.read_run_state", return_value=None):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status.boto3.Session"):
                result = runner.invoke(app, ["status", "--source", "ths"])

    assert result.exit_code == 0
    # No crash — just no last-run line emitted
    assert "last run" not in result.output


# ---------------------------------------------------------------------------
# S3 batch display
# ---------------------------------------------------------------------------


def test_status_s3_batch_with_jobs(s3_batch_config, monkeypatch):
    """Batch mode shows job status icon and progress."""
    monkeypatch.chdir(s3_batch_config)

    mock_job = {
        "JobId": "abcd1234-5678-abcd-efgh-1234567890ab",
        "Status": "Complete",
        "Description": "source-bucket backup",
        "CreationTime": TS,
        "ProgressSummary": {
            "TotalNumberOfTasks": 15,
            "NumberOfTasksFailed": 0,
            "NumberOfTasksSucceeded": 0,  # FailedTasksOnly report
        },
    }

    with patch("aws_snapshot.commands.status.read_run_state", return_value=None):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch(
                "aws_snapshot.commands.status._get_recent_batch_jobs", return_value=[mock_job]
            ):
                with patch("aws_snapshot.commands.status.boto3.Session"):
                    result = runner.invoke(app, ["status", "--source", "arkivalist"])

    assert result.exit_code == 0
    assert "✓" in result.output
    assert "15/15 objects" in result.output


def test_status_s3_batch_no_jobs(s3_batch_config, monkeypatch):
    """Batch mode with no jobs shows 'no batch jobs found'."""
    monkeypatch.chdir(s3_batch_config)

    with patch("aws_snapshot.commands.status.read_run_state", return_value=None):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status._get_recent_batch_jobs", return_value=[]):
                with patch("aws_snapshot.commands.status.boto3.Session"):
                    result = runner.invoke(app, ["status", "--source", "arkivalist"])

    assert result.exit_code == 0
    assert "no batch jobs found" in result.output


def test_status_s3_batch_running_without_job_shows_preparing_message(s3_batch_config, monkeypatch):
    """If a run has started but batch job is not submitted yet, show a clear running message."""
    monkeypatch.chdir(s3_batch_config)

    running_state = {
        "checked_at": "2026-03-18T10:00:00",
        "status": "running",
        "batch_job_id": None,
        "objects_in_manifest": 0,
    }

    with patch("aws_snapshot.commands.status.read_run_state", return_value=running_state):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status._get_recent_batch_jobs", return_value=[]):
                with patch("aws_snapshot.commands.status.boto3.Session"):
                    result = runner.invoke(app, ["status", "--source", "arkivalist"])

    assert result.exit_code == 0
    assert "running (preparing manifest; batch job not submitted yet)" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_status_json_output_structure(dynamo_config, monkeypatch):
    """--output json produces valid JSON keyed by source alias."""
    monkeypatch.chdir(dynamo_config)
    mock_session = MagicMock()
    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[]):
            result = runner.invoke(app, ["status", "--output", "json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "toshi" in data
    assert "dynamodb_tables" in data["toshi"]


def test_status_json_output_includes_s3_batch_jobs(s3_batch_config, monkeypatch):
    """Batch-mode sources include recent S3 Batch jobs in JSON output."""
    monkeypatch.chdir(s3_batch_config)

    mock_job = {
        "JobId": "abcd1234-5678-abcd-efgh-1234567890ab",
        "Status": "Active",
        "Description": "source-bucket backup",
        "CreationTime": TS,
        "ProgressSummary": {
            "TotalNumberOfTasks": 20,
            "NumberOfTasksFailed": 2,
            "NumberOfTasksSucceeded": 8,
        },
    }

    mock_session = MagicMock()
    mock_session.client.return_value = MagicMock()
    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status.read_run_state", return_value=None):
                with patch(
                    "aws_snapshot.commands.status._get_recent_batch_jobs", return_value=[mock_job]
                ):
                    result = runner.invoke(
                        app, ["status", "--source", "arkivalist", "--output", "json"]
                    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "arkivalist" in data
    batches = data["arkivalist"]["s3_batches"]
    assert len(batches) == 1
    assert batches[0]["source_bucket"] == "source-bucket"
    assert "last_run" in batches[0]
    recent = batches[0]["recent_jobs"]
    assert recent[0]["job_id"] == "abcd1234-5678-abcd-efgh-1234567890ab"
    assert recent[0]["status"] == "Active"
    assert recent[0]["total_tasks"] == 20
    assert recent[0]["tasks_succeeded"] == 8
    assert recent[0]["tasks_failed"] == 2


def test_status_job_id_requires_source_alias(s3_batch_config, monkeypatch):
    """--job-id without --source should fail fast."""
    monkeypatch.chdir(s3_batch_config)
    result = runner.invoke(app, ["status", "--job-id", "abcd1234"])
    assert result.exit_code == 1
    assert "requires --source" in result.output


def test_status_job_id_with_non_batch_source_fails(s3_incremental_config, monkeypatch):
    """--job-id for non-batch source should fail with clear error."""
    monkeypatch.chdir(s3_incremental_config)
    result = runner.invoke(app, ["status", "--source", "ths", "--job-id", "abcd1234"])
    assert result.exit_code == 1
    assert "not configured for S3 Batch" in result.output


def test_status_text_with_job_id_prints_selected_job(s3_batch_config, monkeypatch):
    """--job-id prints selected job details in text output."""
    monkeypatch.chdir(s3_batch_config)

    selected_job = {
        "JobId": "abcd1234-5678-abcd-efgh-1234567890ab",
        "Status": "Active",
        "Description": "source-bucket backup",
        "CreationTime": TS,
        "ProgressSummary": {
            "TotalNumberOfTasks": 20,
            "NumberOfTasksFailed": 2,
            "NumberOfTasksSucceeded": 8,
        },
    }

    mock_s3control = MagicMock()
    mock_s3control.describe_job.return_value = {"Job": selected_job}
    mock_session = MagicMock()
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_s3control if svc == "s3control" else MagicMock()
    )

    with patch("aws_snapshot.commands.status.boto3.Session", return_value=mock_session):
        with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
            with patch("aws_snapshot.commands.status.read_run_state", return_value=None):
                with patch("aws_snapshot.commands.status._get_recent_batch_jobs", return_value=[]):
                    result = runner.invoke(
                        app,
                        [
                            "status",
                            "--source",
                            "arkivalist",
                            "--job-id",
                            "abcd1234-5678-abcd-efgh-1234567890ab",
                        ],
                    )

    assert result.exit_code == 0
    assert "Selected S3 Batch job" in result.output
    assert "job/abcd1234-5678-abcd-efgh-1234567890ab" in result.output
    assert "50.0% done" in result.output


# ---------------------------------------------------------------------------
# get_status_dict() — programmatic API for the daily health report
# ---------------------------------------------------------------------------


def test_get_status_dict_returns_dynamodb_data(dynamo_config, monkeypatch):
    """get_status_dict assembles per-source DynamoDB export data without CLI."""
    from aws_snapshot.commands.status import get_status_dict
    from aws_snapshot.config import load_config

    monkeypatch.chdir(dynamo_config)
    config = load_config("backup-config.yaml")
    mock_session = MagicMock()
    mock_export = {
        "ExportArn": "arn:aws:dynamodb:::export/abc",
        "ExportStatus": "COMPLETED",
        "ExportTime": TS,
    }

    with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[mock_export]):
            out = get_status_dict(["toshi"], config, mock_session)

    assert "toshi" in out
    assert "TestTable" in out["toshi"]["dynamodb_tables"]
    assert out["toshi"]["dynamodb_tables"]["TestTable"][0]["status"] == "COMPLETED"
    assert out["toshi"]["s3_batches"] == []


def test_get_status_dict_no_typer_echo(dynamo_config, monkeypatch, capsys):
    """get_status_dict must not emit any stdout/stderr — pure data return."""
    from aws_snapshot.commands.status import get_status_dict
    from aws_snapshot.config import load_config

    monkeypatch.chdir(dynamo_config)
    config = load_config("backup-config.yaml")
    mock_session = MagicMock()

    with patch("aws_snapshot.commands.status.get_account_id", return_value=ACCOUNT_ID):
        with patch("aws_snapshot.commands.status._get_recent_exports", return_value=[]):
            get_status_dict(["toshi"], config, mock_session)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
