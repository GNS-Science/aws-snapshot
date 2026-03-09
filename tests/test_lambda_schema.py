"""Tests for Lambda task schema."""

import pytest

from nzshm_backup.lambda_schema import BackupTask


def test_backup_task_valid():
    """Test valid BackupTask creation."""
    task = BackupTask(
        source="toshi",
        dry_run=False,
        trigger_type="scheduled",
    )

    assert task.source == "toshi"
    assert task.dry_run is False
    assert task.is_scheduled() is True


def test_backup_task_defaults():
    """Test BackupTask default values."""
    task = BackupTask(source="all")

    assert task.dry_run is False
    assert task.trigger_type == "scheduled"
    assert task.full_sync is False
    assert task.is_scheduled() is True


def test_backup_task_invalid_source():
    """Test invalid source raises error."""
    with pytest.raises(ValueError):
        BackupTask(source="invalid_source")


def test_backup_task_extra_fields_forbidden():
    """Test extra fields are forbidden."""
    with pytest.raises(ValueError):
        BackupTask(source="toshi", unknown_field="value")


def test_backup_task_should_sync_all():
    """Test full_sync flag."""
    task_normal = BackupTask(source="toshi")
    task_full = BackupTask(source="toshi", full_sync=True)

    assert task_normal.should_sync_all() is False
    assert task_full.should_sync_all() is True


def test_backup_task_from_dict():
    """Test BackupTask from dictionary."""
    event = {
        "source": "ths",
        "dry_run": True,
        "trigger_type": "manual",
        "full_sync": True,
    }

    task = BackupTask.model_validate(event)

    assert task.source == "ths"
    assert task.dry_run is True
    assert task.trigger_type == "manual"
    assert task.full_sync is True
