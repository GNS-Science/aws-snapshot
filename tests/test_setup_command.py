"""Tests for setup command wrappers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from aws_snapshot.commands.setup import app

runner = CliRunner()


def test_setup_inventory_invokes_script_with_required_args():
    with patch("aws_snapshot.commands.setup.subprocess.run") as run:
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
    with patch("aws_snapshot.commands.setup.subprocess.run") as run:
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


def test_setup_lifecycle_dry_run_lists_buckets_without_applying():
    fake_s3 = MagicMock()
    fake_sts = MagicMock()
    fake_sts.get_caller_identity.return_value = {"Account": "345678901234"}
    fake_session = MagicMock()
    fake_session.client.side_effect = lambda name: fake_sts if name == "sts" else fake_s3

    with patch("aws_snapshot.commands.setup.boto3.Session", return_value=fake_session):
        result = runner.invoke(
            app,
            [
                "lifecycle",
                "--source",
                "all",
                "--config",
                "backup-config.production.yaml",
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "GLACIER_IR" in result.output
    # Production config sets source_account_id=210987654321 for each source;
    # bucket names embed that, not the SSO/backup-account caller id.
    assert "bb-toshi-s3-api-prod-ap-southeast-2-210987654321" in result.output
    assert "bb-toshi-dynamo-ap-southeast-2-210987654321" in result.output
    fake_s3.put_bucket_lifecycle_configuration.assert_not_called()


def test_setup_lifecycle_applies_to_existing_buckets():
    fake_s3 = MagicMock()
    fake_s3.head_bucket.return_value = {}  # bucket_exists returns True
    fake_sts = MagicMock()
    fake_sts.get_caller_identity.return_value = {"Account": "345678901234"}
    fake_session = MagicMock()
    fake_session.client.side_effect = lambda name: fake_sts if name == "sts" else fake_s3

    with patch("aws_snapshot.commands.setup.boto3.Session", return_value=fake_session):
        result = runner.invoke(
            app,
            [
                "lifecycle",
                "--source",
                "toshi",
                "--config",
                "backup-config.production.yaml",
            ],
        )

    assert result.exit_code == 0, result.output
    calls = fake_s3.put_bucket_lifecycle_configuration.call_args_list
    # toshi has one S3 bucket + one DDB export bucket
    assert len(calls) == 2
    for call in calls:
        rules = call.kwargs["LifecycleConfiguration"]["Rules"]
        assert rules[0]["Transitions"] == [{"Days": 30, "StorageClass": "GLACIER_IR"}]
        assert rules[0]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 365}
        assert "Expiration" not in rules[0]


def test_setup_iam_backup_batch_role_invokes_script():
    with patch("aws_snapshot.commands.setup.subprocess.run") as run:
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
