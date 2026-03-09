"""Tests for configuration loading and models."""

from pathlib import Path

import pytest
import yaml

from nzshm_backup.config import ConfigModel, load_config, save_config
from nzshm_backup.config.models import RetentionConfig, SourceConfig


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
                "s3_buckets": ["arn:aws:s3:::test-bucket"],
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
                s3_buckets=["arn:aws:s3:::test-bucket"],
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
    assert config.warm_days == 90
    assert config.cold_days == 365
    assert config.max_age_days == 365


def test_source_config_backup_bucket_name():
    """Test backup bucket name generation."""
    source = SourceConfig(
        display_name="Test",
        s3_buckets=["arn:aws:s3:::my-bucket"],
    )

    bucket_name = source.get_backup_bucket_name(
        "arn:aws:s3:::my-bucket",
        "ap-southeast-2",
        "123456789012",
    )

    assert bucket_name == "my-bucket-backup-ap-southeast-2-123456789012"


def test_config_model_validation(sample_config_dict):
    """Test ConfigModel validation."""
    config = ConfigModel.model_validate(sample_config_dict)

    assert config.general.region == "ap-southeast-2"
    assert len(config.sources) == 1
    assert config.retention.hot_days == 30
