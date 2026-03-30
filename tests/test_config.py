"""Tests for configuration loading and models."""

import json
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from nzshm_backup.config import ConfigModel, load_config, save_config
from nzshm_backup.config.loader import load_config_from_ssm
from nzshm_backup.config.models import RetentionConfig, S3BucketConfig, SourceConfig


@pytest.fixture
def sample_config_dict():
    """Sample configuration dictionary for testing."""
    return {
        "general": {
            "region": "ap-southeast-2",
            "environment": "production",
            "tags": {"Project": "NSHM"},
        },
        "sources": {
            "toshi": {
                "display_name": "ToshiAPI",
                "s3_buckets": [{"arn": "arn:aws:s3:::test-bucket", "label": "test"}],
                "dynamodb_tables": [],
            }
        },
        "retention": {
            "hot_days": 30,
            "warm_days": 90,
            "cold_days": 365,
            "max_age_days": 365,
        },
    }


@pytest.fixture
def temp_config_file(tmp_path, sample_config_dict):
    """Create a temporary config file."""
    config_file = tmp_path / "backup-config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(sample_config_dict, f)
    return config_file


def test_load_config_success(temp_config_file):
    """Test successful config loading."""
    config = load_config(temp_config_file)

    assert config.general.region == "ap-southeast-2"
    assert config.general.environment == "production"
    assert "toshi" in config.sources
    assert config.sources["toshi"].display_name == "ToshiAPI"
    assert config.retention.hot_days == 30


def test_load_config_not_found():
    """Test loading non-existent config file."""
    with pytest.raises(FileNotFoundError):
        load_config(Path("/nonexistent/path.yaml"))


def test_save_config(tmp_path):
    """Test config saving."""
    config = ConfigModel(
        sources={
            "toshi": SourceConfig(
                display_name="ToshiAPI",
                s3_buckets=[S3BucketConfig(arn="arn:aws:s3:::test-bucket", label="test")],
            )
        }
    )

    config_file = tmp_path / "test-config.yaml"
    save_config(config, config_file)

    assert config_file.exists()

    loaded = load_config(config_file)
    assert loaded.sources["toshi"].display_name == "ToshiAPI"


def test_retention_config_defaults():
    """Test retention config default values."""
    config = RetentionConfig()

    assert config.hot_days == 30
    assert config.warm_days == 120
    assert config.cold_days == 365
    assert config.max_age_days == 365


def test_source_config_backup_bucket_name():
    """Backup bucket name uses source_key + label pattern."""
    source = SourceConfig(
        display_name="Test",
        s3_buckets=[S3BucketConfig(arn="arn:aws:s3:::my-bucket", label="main")],
    )

    bucket_name = source.get_backup_bucket_name(
        "main",
        "ap-southeast-2",
        "123456789012",
        "toshi",
    )

    assert bucket_name == "bb-toshi-s3-main-ap-southeast-2-123456789012"
    assert len(bucket_name) <= 63


def test_source_config_dynamodb_backup_bucket_name():
    """DynamoDB export bucket name uses source_key pattern."""
    source = SourceConfig(display_name="Test")

    bucket_name = source.get_dynamodb_backup_bucket_name(
        "arkivalist",
        "ap-southeast-2",
        "456789012345",
    )

    assert bucket_name == "bb-arkivalist-dynamo-ap-southeast-2-456789012345"
    assert len(bucket_name) <= 63


def test_validate_source_account_id_required_with_role_arn():
    """source_account_id is required when source_account_role_arn is set."""
    with pytest.raises(ValueError, match="source_account_id is required"):
        ConfigModel(
            sources={
                "arkivalist": SourceConfig(
                    display_name="Arkivalist",
                    source_account_role_arn="arn:aws:iam::456789012345:role/nzshm-backup-reader",
                )
            }
        )


def test_validate_source_account_id_mismatch_raises():
    """source_account_id must match the account in source_account_role_arn."""
    with pytest.raises(ValueError, match="does not match account"):
        ConfigModel(
            sources={
                "arkivalist": SourceConfig(
                    display_name="Arkivalist",
                    source_account_id="999999999999",
                    source_account_role_arn="arn:aws:iam::456789012345:role/nzshm-backup-reader",
                )
            }
        )


def test_validate_dynamodb_arn_account_mismatch_raises():
    """DynamoDB table ARNs must belong to the declared source_account_id."""
    with pytest.raises(ValueError, match="belongs to account"):
        ConfigModel(
            sources={
                "toshi": SourceConfig(
                    display_name="ToshiAPI",
                    source_account_id="111111111111",
                    dynamodb_tables=["arn:aws:dynamodb:ap-southeast-2:222222222222:table/Foo"],
                )
            }
        )


def test_validate_duplicate_bucket_labels_raises():
    """Duplicate s3_bucket labels within a source must be rejected."""
    with pytest.raises(ValueError, match="labels must be unique"):
        ConfigModel(
            sources={
                "toshi": SourceConfig(
                    display_name="ToshiAPI",
                    s3_buckets=[
                        S3BucketConfig(arn="arn:aws:s3:::bucket-a", label="same"),
                        S3BucketConfig(arn="arn:aws:s3:::bucket-b", label="same"),
                    ],
                )
            }
        )


def test_config_model_validation(sample_config_dict):
    """Test ConfigModel validation."""
    config = ConfigModel.model_validate(sample_config_dict)

    assert config.general.region == "ap-southeast-2"
    assert len(config.sources) == 1
    assert config.retention.hot_days == 30


# ---------------------------------------------------------------------------
# SSM loader tests
# ---------------------------------------------------------------------------


@pytest.fixture
def ssm_config_json(sample_config_dict):
    """Return a JSON string of a valid config."""
    config = ConfigModel.model_validate(sample_config_dict)
    return json.dumps(config.model_dump(mode="json", by_alias=True))


def test_load_config_from_ssm(aws_credentials, ssm_config_json):
    """load_config_from_ssm returns a valid ConfigModel when the parameter exists."""
    with mock_aws():
        ssm = boto3.client("ssm", region_name="ap-southeast-2")
        ssm.put_parameter(Name="/nzshm-backup/dev/config", Value=ssm_config_json, Type="String")

        config = load_config_from_ssm("dev")

        assert config.general.region == "ap-southeast-2"
        assert "toshi" in config.sources


def test_load_config_from_ssm_not_found(aws_credentials):
    """load_config_from_ssm raises FileNotFoundError when the parameter is missing."""
    with mock_aws():
        with pytest.raises(FileNotFoundError, match="/nzshm-backup/missing/config"):
            load_config_from_ssm("missing")


# ---------------------------------------------------------------------------
# CLI push / pull command tests
# ---------------------------------------------------------------------------


def test_config_push_uploads_to_ssm(aws_credentials, cli_runner, temp_config_file):
    """push command uploads config to SSM as JSON."""
    from nzshm_backup.commands.config import app

    with mock_aws():
        result = cli_runner.invoke(app, ["push", str(temp_config_file), "--stage", "dev"])
        assert result.exit_code == 0, result.output
        assert "/nzshm-backup/dev/config" in result.output

        ssm = boto3.client("ssm", region_name="ap-southeast-2")
        response = ssm.get_parameter(Name="/nzshm-backup/dev/config")
        stored = json.loads(response["Parameter"]["Value"])
        assert "sources" in stored
        assert "toshi" in stored["sources"]


def test_config_push_dry_run(aws_credentials, cli_runner, temp_config_file):
    """push --dry-run prints a preview but does NOT create the SSM parameter."""
    from nzshm_backup.commands.config import app

    with mock_aws():
        result = cli_runner.invoke(
            app,
            ["push", str(temp_config_file), "--stage", "dev", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "[dry-run]" in result.output

        # Parameter must NOT have been created
        ssm = boto3.client("ssm", region_name="ap-southeast-2")
        with pytest.raises(ssm.exceptions.ParameterNotFound):
            ssm.get_parameter(Name="/nzshm-backup/dev/config")


def test_config_pull_shows_config(aws_credentials, cli_runner, ssm_config_json):
    """pull command fetches config from SSM and prints it as YAML."""
    from nzshm_backup.commands.config import app

    with mock_aws():
        ssm = boto3.client("ssm", region_name="ap-southeast-2")
        ssm.put_parameter(Name="/nzshm-backup/dev/config", Value=ssm_config_json, Type="String")

        result = cli_runner.invoke(app, ["pull", "--stage", "dev"])

        assert result.exit_code == 0, result.output
        # YAML output should contain the source name
        assert "toshi" in result.output
