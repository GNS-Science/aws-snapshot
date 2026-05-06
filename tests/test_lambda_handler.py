"""Tests for the Lambda handler entry point."""

import json
import os
from unittest.mock import patch

import pytest
from moto import mock_aws

from nzshm_backup.config.models import ConfigModel, GeneralConfig, SourceConfig
from nzshm_backup.lambda_handler import handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def _set_region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _make_config(**source_kwargs) -> ConfigModel:
    return ConfigModel(
        general=GeneralConfig(region=REGION),
        sources={
            "testsrc": SourceConfig(
                display_name="Test Source",
                s3_buckets=source_kwargs.get("s3_buckets", []),
                dynamodb_tables=source_kwargs.get("dynamodb_tables", []),
            )
        },
    )


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------


def test_handler_invalid_event_returns_400():
    """Event missing required 'source' field → 400."""
    result = handler({"not_a_source": "foo"}, None)
    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert "Invalid event format" in body["error"]


def test_handler_extra_fields_forbidden_returns_400():
    """Event with extra fields forbidden by schema → 400."""
    result = handler({"source": "testsrc", "unknown_field": "bad"}, None)
    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Successful runs
# ---------------------------------------------------------------------------


@mock_aws
def test_handler_valid_event_empty_source_returns_200():
    """Valid event, source with no buckets/tables → 200 with success=True."""
    with patch("nzshm_backup.lambda_handler.get_config", return_value=_make_config()):
        event = {"source": "testsrc", "dry_run": True, "trigger_type": "manual"}
        result = handler(event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["success"] is True
    assert body["task"]["source"] == "testsrc"
    assert body["task"]["dry_run"] is True


@mock_aws
def test_handler_all_sources_runs_each():
    """source='all' iterates every source in config."""
    config = ConfigModel(
        general=GeneralConfig(region=REGION),
        sources={
            "src1": SourceConfig(display_name="Source 1"),
            "src2": SourceConfig(display_name="Source 2"),
        },
    )
    with patch("nzshm_backup.lambda_handler.get_config", return_value=config):
        result = handler({"source": "all", "dry_run": True}, None)

    assert result["statusCode"] == 200


@mock_aws
def test_handler_defaults_dry_run_false():
    """dry_run defaults to False when omitted from event."""
    with patch("nzshm_backup.lambda_handler.get_config", return_value=_make_config()):
        result = handler({"source": "testsrc"}, None)

    body = json.loads(result["body"])
    assert body["task"]["dry_run"] is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@mock_aws
def test_handler_unknown_source_captured_in_results():
    """Unknown source alias → error recorded per-source, not an unhandled exception."""
    with patch("nzshm_backup.lambda_handler.get_config", return_value=_make_config()):
        result = handler({"source": "no_such_source", "dry_run": True}, None)

    body = json.loads(result["body"])
    assert "no_such_source" in body["results"]
    assert "error" in body["results"]["no_such_source"]


def test_handler_config_load_failure_returns_500():
    """Exception raised by get_config → 500 with error message."""
    with patch("nzshm_backup.lambda_handler.get_config", side_effect=Exception("SSM unreachable")):
        result = handler({"source": "testsrc", "dry_run": True}, None)

    assert result["statusCode"] == 500
    body = json.loads(result["body"])
    assert "SSM unreachable" in body["error"]


# ---------------------------------------------------------------------------
# get_config resolution order
# ---------------------------------------------------------------------------


def test_get_config_uses_ssm_when_stage_set(monkeypatch, tmp_path):
    """NZSHM_STAGE set → tries SSM first, falls back to env/file on FileNotFoundError."""
    from nzshm_backup.lambda_handler import get_config

    monkeypatch.setenv("NZSHM_STAGE", "test")

    fallback_config = _make_config()
    with patch(
        "nzshm_backup.lambda_handler.load_config_from_ssm",
        side_effect=FileNotFoundError("no ssm param"),
    ):
        with patch(
            "nzshm_backup.lambda_handler.load_config_from_env", side_effect=ValueError("no env")
        ):
            with patch(
                "nzshm_backup.lambda_handler.load_config", return_value=fallback_config
            ) as mock_file:
                result = get_config()

    mock_file.assert_called_once()
    assert result is fallback_config


def test_get_config_uses_env_when_no_stage(monkeypatch):
    """No NZSHM_STAGE → skips SSM, tries env config."""
    from nzshm_backup.lambda_handler import get_config

    monkeypatch.delenv("NZSHM_STAGE", raising=False)
    fallback_config = _make_config()

    with patch(
        "nzshm_backup.lambda_handler.load_config_from_env", return_value=fallback_config
    ) as mock_env:
        result = get_config()

    mock_env.assert_called_once()
    assert result is fallback_config
