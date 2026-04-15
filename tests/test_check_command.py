"""Tests for the backup check pre-flight command."""

from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from nzshm_backup.cli import app

runner = CliRunner()

REGION = "ap-southeast-2"
ACCOUNT_ID = "123456789012"
SOURCE_ACCOUNT_ID = "999888777666"
READER_ROLE_ARN = f"arn:aws:iam::{SOURCE_ACCOUNT_ID}:role/nzshm-backup-reader"
BATCH_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/nzshm-backup-batch-role"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def same_account_s3_config(tmp_path):
    """Single S3 source, same account, no batch."""
    cfg = {
        "general": {"region": REGION},
        "sources": {
            "ths": {
                "display_name": "THS",
                "s3_buckets": [{"arn": "arn:aws:s3:::ths-dataset-prod", "label": "dataset"}],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


@pytest.fixture
def cross_account_batch_config(tmp_path):
    """Cross-account S3 Batch source with DynamoDB."""
    cfg = {
        "general": {
            "region": REGION,
            "s3_batch_role_arn": BATCH_ROLE_ARN,
        },
        "sources": {
            "toshi": {
                "display_name": "ToshiAPI",
                "source_account_id": SOURCE_ACCOUNT_ID,
                "source_account_role_arn": READER_ROLE_ARN,
                "use_s3_batch": True,
                "s3_buckets": [{"arn": "arn:aws:s3:::nzshm22-toshi-api-prod", "label": "api"}],
                "dynamodb_tables": [
                    f"arn:aws:dynamodb:{REGION}:{SOURCE_ACCOUNT_ID}:table/ToshiFileObject-PROD"
                ],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


@pytest.fixture
def cross_account_batch_config_no_batch_role(tmp_path):
    """Cross-account source with use_s3_batch=True but s3_batch_role_arn missing."""
    cfg = {
        "general": {"region": REGION},
        "sources": {
            "toshi": {
                "display_name": "ToshiAPI",
                "source_account_id": SOURCE_ACCOUNT_ID,
                "source_account_role_arn": READER_ROLE_ARN,
                "use_s3_batch": True,
                "s3_buckets": [{"arn": "arn:aws:s3:::nzshm22-toshi-api-prod", "label": "api"}],
            }
        },
    }
    p = tmp_path / "backup-config.yaml"
    p.write_text(yaml.dump(cfg))
    return tmp_path


# ---------------------------------------------------------------------------
# Error / guard cases
# ---------------------------------------------------------------------------


def test_check_missing_config_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1


def test_check_unknown_source_exits_nonzero(same_account_s3_config, monkeypatch):
    monkeypatch.chdir(same_account_s3_config)
    result = runner.invoke(app, ["check", "--source", "nonexistent"])
    assert result.exit_code == 1
    assert "unknown source" in result.output


# ---------------------------------------------------------------------------
# Happy path — same-account S3
# ---------------------------------------------------------------------------


def test_check_same_account_s3_all_pass(same_account_s3_config, monkeypatch):
    """All checks pass for a simple same-account S3 source."""
    monkeypatch.chdir(same_account_s3_config)

    mock_session = MagicMock()
    mock_s3 = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID, "Arn": "arn:aws:iam::..."}
    mock_s3.list_objects_v2.return_value = {"Contents": []}
    mock_s3.head_bucket.side_effect = _client_error("404")  # backup bucket doesn't exist yet
    mock_session.client.side_effect = lambda svc, **kw: mock_sts if svc == "sts" else mock_s3

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        result = runner.invoke(app, ["check", "--source", "ths"])

    assert result.exit_code == 0
    assert "PASS" in result.output
    assert "FAIL" not in result.output
    assert "All checks passed" in result.output


# ---------------------------------------------------------------------------
# Credential failure
# ---------------------------------------------------------------------------


def test_check_bad_backup_credentials_exits_nonzero(same_account_s3_config, monkeypatch):
    """If backup account credentials fail, check exits 1 immediately."""
    monkeypatch.chdir(same_account_s3_config)

    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.side_effect = _client_error("InvalidClientTokenId")
    mock_session.client.return_value = mock_sts

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        result = runner.invoke(app, ["check", "--source", "ths"])

    assert result.exit_code == 1
    assert "FAIL" in result.output


# ---------------------------------------------------------------------------
# Cross-account role
# ---------------------------------------------------------------------------


def test_check_cross_account_role_pass(cross_account_batch_config, monkeypatch):
    """Successful cross-account role assumption is shown as PASS."""
    monkeypatch.chdir(cross_account_batch_config)

    mock_session = MagicMock()
    mock_source_session = MagicMock()

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID, "Arn": "arn:backup"}
    mock_source_sts = MagicMock()
    mock_source_sts.get_caller_identity.return_value = {
        "Account": SOURCE_ACCOUNT_ID,
        "Arn": f"arn:aws:sts::{SOURCE_ACCOUNT_ID}:assumed-role/nzshm-backup-reader/nzshm-backup",
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {}
    mock_s3.head_bucket.side_effect = _client_error("404")

    mock_iam = MagicMock()
    mock_iam.get_role.return_value = {"Role": {"Arn": BATCH_ROLE_ARN}}

    mock_dynamo = MagicMock()
    mock_dynamo.describe_continuous_backups.return_value = {
        "ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
        }
    }

    mock_session.client.side_effect = lambda svc, **kw: {
        "sts": mock_sts, "s3": mock_s3, "iam": mock_iam
    }.get(svc, MagicMock())
    mock_source_session.client.side_effect = lambda svc, **kw: {
        "sts": mock_source_sts, "s3": mock_s3, "dynamodb": mock_dynamo
    }.get(svc, MagicMock())

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        with patch(
            "nzshm_backup.commands.check.get_cross_account_session",
            return_value=mock_source_session,
        ):
            result = runner.invoke(app, ["check", "--source", "toshi"])

    assert result.exit_code == 0
    assert "FAIL" not in result.output
    assert "nzshm-backup-reader" in result.output


def test_check_cross_account_role_fail(cross_account_batch_config, monkeypatch):
    """Failed role assumption is shown as FAIL and exits 1."""
    monkeypatch.chdir(cross_account_batch_config)

    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID, "Arn": "arn:backup"}
    mock_session.client.return_value = mock_sts

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        with patch(
            "nzshm_backup.commands.check.get_cross_account_session",
            side_effect=_client_error("AccessDenied"),
        ):
            result = runner.invoke(app, ["check", "--source", "toshi"])

    assert result.exit_code == 1
    assert "FAIL" in result.output


# ---------------------------------------------------------------------------
# Source bucket access failure
# ---------------------------------------------------------------------------


def test_check_source_bucket_unreadable(same_account_s3_config, monkeypatch):
    """AccessDenied on source bucket list shows FAIL."""
    monkeypatch.chdir(same_account_s3_config)

    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID, "Arn": "arn:backup"}
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.side_effect = _client_error("AccessDenied")
    mock_s3.head_bucket.side_effect = _client_error("404")
    mock_session.client.side_effect = lambda svc, **kw: mock_sts if svc == "sts" else mock_s3

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        result = runner.invoke(app, ["check", "--source", "ths"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "ths-dataset-prod" in result.output


# ---------------------------------------------------------------------------
# S3 Batch role missing
# ---------------------------------------------------------------------------


def test_check_batch_role_missing(cross_account_batch_config_no_batch_role, monkeypatch):
    """use_s3_batch=True with no s3_batch_role_arn is caught at config load time → exit 1."""
    monkeypatch.chdir(cross_account_batch_config_no_batch_role)
    # No AWS mocks needed — Pydantic rejects the config before any AWS calls
    result = runner.invoke(app, ["check", "--source", "toshi"])
    assert result.exit_code == 1
    assert "s3_batch_role_arn" in result.output


# ---------------------------------------------------------------------------
# DynamoDB PITR
# ---------------------------------------------------------------------------


def test_check_pitr_disabled_is_warn_not_fail(cross_account_batch_config, monkeypatch):
    """PITR DISABLED is a WARN, not a FAIL — check exits 0."""
    monkeypatch.chdir(cross_account_batch_config)

    mock_session = MagicMock()
    mock_source_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID, "Arn": "arn:backup"}
    mock_source_sts = MagicMock()
    mock_source_sts.get_caller_identity.return_value = {"Account": SOURCE_ACCOUNT_ID, "Arn": "arn:src"}
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {}
    mock_s3.head_bucket.side_effect = _client_error("404")
    mock_iam = MagicMock()
    mock_iam.get_role.return_value = {"Role": {"Arn": BATCH_ROLE_ARN}}
    mock_dynamo = MagicMock()
    mock_dynamo.describe_continuous_backups.return_value = {
        "ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "DISABLED"}
        }
    }

    mock_session.client.side_effect = lambda svc, **kw: {
        "sts": mock_sts, "s3": mock_s3, "iam": mock_iam
    }.get(svc, MagicMock())
    mock_source_session.client.side_effect = lambda svc, **kw: {
        "sts": mock_source_sts, "s3": mock_s3, "dynamodb": mock_dynamo
    }.get(svc, MagicMock())

    with patch("nzshm_backup.commands.check.boto3.Session", return_value=mock_session):
        with patch(
            "nzshm_backup.commands.check.get_cross_account_session",
            return_value=mock_source_session,
        ):
            result = runner.invoke(app, ["check", "--source", "toshi"])

    assert result.exit_code == 0
    assert "WARN" in result.output
    assert "DISABLED" in result.output
    assert "All checks passed" in result.output
