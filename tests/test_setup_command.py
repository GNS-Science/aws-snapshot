"""Tests for setup command wrappers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from nzshm_backup.commands.setup import app

runner = CliRunner()


def test_setup_inventory_invokes_script_with_required_args():
    with patch("nzshm_backup.commands.setup.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        result = runner.invoke(
            app,
            [
                "inventory",
                "--source",
                "ths",
                "--config",
                "backup-config.production.yaml",
                "--source-profile",
                "nshm-admin",
                "--backup-profile",
                "nshm-backup-admin",
            ],
        )

    assert result.exit_code == 0
    called = run.call_args.args[0]
    assert "setup-inventory.py" in " ".join(called)
    assert "--source" in called
    assert "ths" in called


def test_setup_iam_source_roles_invokes_script():
    with patch("nzshm_backup.commands.setup.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        result = runner.invoke(
            app,
            [
                "iam",
                "source-roles",
                "--source",
                "toshi",
                "--profile",
                "nshm-admin",
                "--config",
                "backup-config.production.yaml",
            ],
        )

    assert result.exit_code == 0
    called = run.call_args.args[0]
    assert "create-source-roles.py" in " ".join(called)


def test_setup_iam_backup_batch_role_invokes_script():
    with patch("nzshm_backup.commands.setup.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        result = runner.invoke(
            app,
            [
                "iam",
                "backup-batch-role",
                "--profile",
                "nshm-backup-admin",
                "--config",
                "backup-config.production.yaml",
            ],
        )

    assert result.exit_code == 0
    called = run.call_args.args[0]
    assert "create-backup-roles.py" in " ".join(called)
