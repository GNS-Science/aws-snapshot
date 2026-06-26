"""Smoke tests for the backup CLI entry point and subcommands."""

import boto3
import pytest
import yaml
from moto import mock_aws
from typer.testing import CliRunner

from aws_snapshot import __version__
from aws_snapshot.cli import app

runner = CliRunner()


@pytest.fixture
def temp_config(tmp_path):
    """Create a temporary config file for CLI tests."""
    config_data = {
        "general": {
            "region": "ap-southeast-2",
            "environment": "staging",
        },
        "sources": {
            "toshi": {
                "display_name": "ToshiAPI",
                "s3_buckets": [{"arn": "arn:aws:s3:::test-toshi-bucket", "label": "test"}],
            },
            "ths": {
                "display_name": "THS_dataset_prod",
                "s3_buckets": [{"arn": "arn:aws:s3:::test-ths-bucket", "label": "test"}],
            },
        },
        "retention": {
            "hot_days": 30,
        },
    }
    config_file = tmp_path / "backup-config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    return config_file


def test_help_exits_cleanly():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "NSHM Backup Solution" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.parametrize(
    "subcommand",
    [
        ["schedule", "--help"],
        ["run", "--help"],
        ["restore", "--help"],
        ["test", "--help"],
        ["status", "--help"],
        ["report", "--help"],
        ["costs", "--help"],
        ["config", "--help"],
    ],
)
def test_subcommand_help(subcommand):
    result = runner.invoke(app, subcommand)
    assert result.exit_code == 0


@mock_aws
def test_dry_run_flag_propagates(temp_config, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.chdir(temp_config.parent)
    s3 = boto3.client("s3", region_name="ap-southeast-2")
    s3.create_bucket(
        Bucket="test-toshi-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )
    result = runner.invoke(app, ["--dry-run", "run", "--source", "toshi"])
    assert result.exit_code == 0, f"Exit code: {result.exit_code}, Output: {result.output}"
    assert "[DRY RUN]" in result.output


@mock_aws
def test_run_without_dry_run(temp_config, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.chdir(temp_config.parent)
    s3 = boto3.client("s3", region_name="ap-southeast-2")
    s3.create_bucket(
        Bucket="test-ths-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )
    result = runner.invoke(app, ["run", "--source", "ths"])
    assert result.exit_code == 0, f"Exit code: {result.exit_code}, Output: {result.output}"
    assert "DRY RUN" not in result.output


def test_costs_subcommands_exist():
    for sub in ["predict", "report", "breakdown", "export"]:
        result = runner.invoke(app, ["costs", sub, "--help"])
        assert result.exit_code == 0, f"costs {sub} --help failed: {result.output}"


def test_report_compliance_exists():
    result = runner.invoke(app, ["report", "compliance", "--help"])
    assert result.exit_code == 0
