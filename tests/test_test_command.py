"""Tests for the test (validation) commands module."""

from unittest.mock import MagicMock, patch  # noqa: I001

import pytest

from aws_snapshot.commands.test import (
    _delete_temp_bucket,
    _fmt_dt,
    _verify_restored_object,
)
from aws_snapshot.integrity import get_object_checksum


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Reset singleton state before each test to avoid dry_run leaks."""
    from aws_snapshot.state import _state

    _state.dry_run = False
    _state.verbose = False
    _state.output = "text"
    yield


# ---------------------------------------------------------------------------
# _fmt_dt
# ---------------------------------------------------------------------------


def test_fmt_dt_with_string():
    result = _fmt_dt("2026-04-30T12:00:00+00:00")
    assert "2026" in result
    # UTC 12:00 converts to local time — just verify it parsed and formatted
    assert ":" in result


# ---------------------------------------------------------------------------
# get_object_checksum
# ---------------------------------------------------------------------------


class TestGetObjectChecksum:
    """Tests for get_object_checksum helper."""

    def test_returns_first_available_checksum(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumSHA256": "abc123"},
        }
        result = get_object_checksum(s3, "bucket", "key")
        assert result == ("ChecksumSHA256", "abc123")

    def test_returns_crc64_when_present(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumCRC64NVME": "crc64val", "ChecksumSHA256": "sha256val"},
        }
        result = get_object_checksum(s3, "bucket", "key")
        # CRC64NVME is first in _CHECKSUM_KEYS, so it wins
        assert result == ("ChecksumCRC64NVME", "crc64val")

    def test_returns_none_when_no_checksum(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {"Checksum": {}}
        result = get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_when_empty_value(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumSHA256": ""},
        }
        result = get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_on_exception(self):
        s3 = MagicMock()
        s3.get_object_attributes.side_effect = Exception("AccessDenied")
        result = get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_when_checksum_key_missing(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {}
        result = get_object_checksum(s3, "bucket", "key")
        assert result is None


# ---------------------------------------------------------------------------
# _verify_restored_object
# ---------------------------------------------------------------------------


class TestVerifyRestoredObject:
    """Tests for _verify_restored_object helper."""

    def test_checksum_match_returns_none(self):
        s3 = MagicMock()
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), ("ChecksumSHA256", "abc")],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is None

    def test_checksum_mismatch_returns_error(self):
        s3 = MagicMock()
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), ("ChecksumSHA256", "xyz")],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is not None
        assert "mismatch" in result
        assert "abc" in result
        assert "xyz" in result

    def test_different_algorithms_falls_to_etag(self):
        """When source and target have different checksum algorithms, fall back to ETag."""
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"etag"'}
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), ("ChecksumCRC32", "def")],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is None
        s3.head_object.assert_called_once()

    def test_etag_fallback_match(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"etag123"'}
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            return_value=None,
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag123"')
        assert result is None

    def test_etag_fallback_mismatch(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"different"'}
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            return_value=None,
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"expected"')
        assert result is not None
        assert "ETag mismatch" in result

    def test_no_target_checksum_falls_to_etag(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"etag"'}
        with patch(
            "aws_snapshot.commands.test.get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), None],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is None


# ---------------------------------------------------------------------------
# _delete_temp_bucket
# ---------------------------------------------------------------------------


class TestDeleteTempBucket:
    """Tests for _delete_temp_bucket helper."""

    def test_deletes_objects_then_bucket(self):
        s3 = MagicMock()
        s3.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "obj1"},
                    {"Key": "obj2"},
                ]
            }
        ]
        _delete_temp_bucket(s3, "temp-bucket")
        s3.delete_objects.assert_called_once_with(
            Bucket="temp-bucket",
            Delete={"Objects": [{"Key": "obj1"}, {"Key": "obj2"}]},
        )
        s3.delete_bucket.assert_called_once_with(Bucket="temp-bucket")

    def test_deletes_empty_bucket(self):
        s3 = MagicMock()
        s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
        _delete_temp_bucket(s3, "temp-bucket")
        s3.delete_objects.assert_not_called()
        s3.delete_bucket.assert_called_once_with(Bucket="temp-bucket")

    def test_handles_exception_gracefully(self):
        s3 = MagicMock()
        s3.get_paginator.side_effect = Exception("NoSuchBucket")
        # Should not raise
        _delete_temp_bucket(s3, "temp-bucket")

    def test_multi_page_deletion(self):
        s3 = MagicMock()
        s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "a"}]},
            {"Contents": [{"Key": "b"}]},
        ]
        _delete_temp_bucket(s3, "temp-bucket")
        assert s3.delete_objects.call_count == 2
        s3.delete_bucket.assert_called_once()


# ---------------------------------------------------------------------------
# test_integrity command
# ---------------------------------------------------------------------------


class TestIntegrityCommand:
    """Tests for the test_integrity CLI command."""

    @patch("aws_snapshot.commands.test.load_config")
    def test_unknown_source_exits(self, mock_config):
        """Unknown source should exit with code 1."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = MagicMock()
        cfg.sources = {"valid-source": MagicMock()}
        mock_config.return_value = cfg

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "nonexistent"])
        assert result.exit_code == 1
        assert "unknown source" in result.output.lower()

    @patch("aws_snapshot.commands.test.load_config")
    def test_config_not_found_exits(self, mock_config):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        mock_config.side_effect = FileNotFoundError("no config")
        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "foo"])
        assert result.exit_code == 1

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_inventory_mode_shows_warning(
        self, mock_config, mock_boto, mock_account, mock_integrity
    ):
        """Inventory-mode sources should show a slow-listing warning."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        source_cfg = MagicMock()
        source_cfg.s3_buckets = [MagicMock()]
        source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
        source_cfg.s3_buckets[0].label = "src"
        source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
        source_cfg.batch_manifest_mode = "inventory"
        source_cfg.source_account_id = None
        source_cfg.source_account_role_arn = None
        source_cfg.dynamodb_tables = []

        cfg = MagicMock()
        cfg.sources = {"mysource": source_cfg}
        cfg.general.region = "ap-southeast-2"
        mock_config.return_value = cfg

        integrity_result = MagicMock()
        integrity_result.clean = True
        integrity_result.errors = []
        integrity_result.source_object_count = 10
        integrity_result.backup_object_count = 10
        mock_integrity.return_value = integrity_result

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])
        assert "Integrity check" in (result.output + result.stderr)


# ---------------------------------------------------------------------------
# test_restore command
# ---------------------------------------------------------------------------


class TestRestoreCommand:
    """Tests for the test_restore CLI command."""

    @patch("aws_snapshot.commands.test.load_config")
    def test_unknown_source_exits(self, mock_config):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = MagicMock()
        cfg.sources = {"valid-source": MagicMock()}
        mock_config.return_value = cfg

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "nonexistent"])
        assert result.exit_code == 1

    @patch("aws_snapshot.commands.test.load_config")
    def test_config_not_found_exits(self, mock_config):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        mock_config.side_effect = FileNotFoundError("no config")
        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "foo"])
        assert result.exit_code == 1

    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test._delete_temp_bucket_silent", return_value=None)
    @patch("aws_snapshot.commands.test._verify_restored_object", return_value=None)
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_inventory_sampling_path(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_verify,
        mock_delete,
        mock_event,
    ):
        """Inventory-mode sources should sample via Athena."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        source_cfg = MagicMock()
        source_cfg.s3_buckets = [MagicMock()]
        source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
        source_cfg.s3_buckets[0].label = "src"
        source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
        source_cfg.batch_manifest_mode = "inventory"
        source_cfg.source_account_id = None
        source_cfg.source_account_role_arn = None
        source_cfg.dynamodb_tables = []

        cfg = MagicMock()
        cfg.sources = {"mysource": source_cfg}
        cfg.general.region = "ap-southeast-2"
        cfg.general.s3_batch_role_arn = None
        mock_config.return_value = cfg

        sample_data = [
            {"Key": "file1.txt", "ETag": '"etag1"', "Size": 100},
            {"Key": "file2.txt", "ETag": '"etag2"', "Size": 200},
        ]

        s3_mock = MagicMock()
        mock_boto.Session.return_value.client.return_value = s3_mock

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            return_value=sample_data,
        ) as mock_sample:
            runner = CliRunner()
            runner.invoke(app, ["restore", "--source", "mysource", "--sample-size", "2"])

        # Should have called the inventory sampler
        mock_sample.assert_called_once()
        # Should have copied and verified
        assert s3_mock.copy_object.call_count == 2
        mock_verify.assert_called()

    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_inventory_unavailable_guard(self, mock_config, mock_boto, mock_account):
        """When inventory is unavailable, should refuse with message."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        source_cfg = MagicMock()
        source_cfg.s3_buckets = [MagicMock()]
        source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
        source_cfg.s3_buckets[0].label = "src"
        source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
        source_cfg.batch_manifest_mode = "inventory"
        source_cfg.source_account_id = None
        source_cfg.source_account_role_arn = None
        source_cfg.dynamodb_tables = []

        cfg = MagicMock()
        cfg.sources = {"mysource": source_cfg}
        cfg.general.region = "ap-southeast-2"
        cfg.general.s3_batch_role_arn = None
        mock_config.return_value = cfg

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            side_effect=ValueError("No inventory data"),
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert result.exit_code == 1
        assert "Inventory unavailable" in result.stderr

    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_dry_run_skips_copy(self, mock_config, mock_boto, mock_account):
        """Dry run should not create temp bucket or copy objects."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        source_cfg = MagicMock()
        source_cfg.s3_buckets = [MagicMock()]
        source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
        source_cfg.s3_buckets[0].label = "src"
        source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
        source_cfg.batch_manifest_mode = "inline"
        source_cfg.source_account_id = None
        source_cfg.source_account_role_arn = None
        source_cfg.dynamodb_tables = []

        cfg = MagicMock()
        cfg.sources = {"mysource": source_cfg}
        cfg.general.region = "ap-southeast-2"
        cfg.general.s3_batch_role_arn = None
        mock_config.return_value = cfg

        s3_mock = MagicMock()
        s3_mock.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "f1.txt", "ETag": '"e1"', "StorageClass": "STANDARD"},
                ]
            }
        ]
        mock_boto.Session.return_value.client.return_value = s3_mock

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "mysource", "--dry-run"])
        assert "DRY RUN" in result.output
        s3_mock.create_bucket.assert_not_called()

    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_use_batch_requires_role_arn(self, mock_config, mock_boto, mock_account):
        """--use-batch without s3_batch_role_arn should exit with error."""
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        source_cfg = MagicMock()
        source_cfg.s3_buckets = []
        source_cfg.source_account_id = None
        source_cfg.source_account_role_arn = None
        source_cfg.dynamodb_tables = []

        cfg = MagicMock()
        cfg.sources = {"mysource": source_cfg}
        cfg.general.region = "ap-southeast-2"
        cfg.general.s3_batch_role_arn = None
        mock_config.return_value = cfg

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "mysource", "--use-batch"])
        assert result.exit_code == 1
        assert "s3_batch_role_arn" in result.stderr


# ---------------------------------------------------------------------------
# test_integrity — dirty results, DynamoDB checks
# ---------------------------------------------------------------------------


def _make_integrity_mocks(source_cfg_overrides=None):
    """Build a standard mock config for integrity tests."""
    source_cfg = MagicMock()
    source_cfg.s3_buckets = [MagicMock()]
    source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
    source_cfg.s3_buckets[0].label = "src"
    source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
    source_cfg.batch_manifest_mode = "inline"
    source_cfg.source_account_id = None
    source_cfg.source_account_role_arn = None
    source_cfg.dynamodb_tables = []
    if source_cfg_overrides:
        for k, v in source_cfg_overrides.items():
            setattr(source_cfg, k, v)

    cfg = MagicMock()
    cfg.sources = {"mysource": source_cfg}
    cfg.general.region = "ap-southeast-2"
    return cfg, source_cfg


class TestIntegrityDirtyResults:
    """Test integrity command with missing/mismatched objects."""

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_missing_objects_reported(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, _ = _make_integrity_mocks()
        mock_config.return_value = cfg

        result_obj = MagicMock()
        result_obj.clean = False
        result_obj.source_object_count = 10
        result_obj.backup_object_count = 8
        result_obj.missing_count = 2
        result_obj.mismatch_count = 0
        result_obj.errors = []
        diff1 = MagicMock(issue="missing_in_backup", key="missing1.txt")
        diff2 = MagicMock(issue="missing_in_backup", key="missing2.txt")
        result_obj.diffs = [diff1, diff2]
        mock_integrity.return_value = result_obj

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert result.exit_code == 1
        assert "missing1.txt" in result.output
        assert "missing2.txt" in result.output
        assert "2 object(s) missing" in result.output

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_etag_mismatches_reported(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, _ = _make_integrity_mocks()
        mock_config.return_value = cfg

        result_obj = MagicMock()
        result_obj.clean = False
        result_obj.source_object_count = 10
        result_obj.backup_object_count = 10
        result_obj.missing_count = 0
        result_obj.mismatch_count = 1
        result_obj.errors = []
        diff1 = MagicMock(
            issue="etag_mismatch",
            key="bad.txt",
            source_etag='"aaa"',
            backup_etag='"bbb"',
        )
        result_obj.diffs = [diff1]
        mock_integrity.return_value = result_obj

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert result.exit_code == 1
        assert "bad.txt" in result.output
        assert "mismatch" in result.output.lower()

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_integrity_errors_reported(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, _ = _make_integrity_mocks()
        mock_config.return_value = cfg

        result_obj = MagicMock()
        result_obj.clean = True
        result_obj.source_object_count = 10
        result_obj.backup_object_count = 10
        result_obj.errors = ["ListBucket failed: AccessDenied"]
        mock_integrity.return_value = result_obj

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert result.exit_code == 1
        assert "AccessDenied" in result.output


class TestIntegrityDynamoDB:
    """Test integrity DynamoDB PITR and export checks."""

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_dynamodb_pitr_enabled(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, source_cfg = _make_integrity_mocks()
        source_cfg.s3_buckets = []
        source_cfg.dynamodb_tables = ["arn:aws:dynamodb:ap-southeast-2:123:table/MyTable"]
        mock_config.return_value = cfg

        mock_dynamo = MagicMock()
        mock_dynamo.describe_continuous_backups.return_value = {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": "ENABLED",
                    "LatestRestorableDateTime": "2026-05-01T00:00:00+00:00",
                }
            }
        }
        mock_dynamo.list_exports.return_value = {
            "ExportSummaries": [
                {"ExportStatus": "COMPLETED", "ExportTime": "2026-05-01T00:00:00+00:00"}
            ]
        }
        # Session().client() must be called without source_account_role_arn
        mock_boto.Session.return_value.client.return_value = mock_dynamo

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert "PITR enabled" in result.output
        assert "1 completed export" in result.output

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_dynamodb_pitr_disabled(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, source_cfg = _make_integrity_mocks()
        source_cfg.s3_buckets = []
        source_cfg.dynamodb_tables = ["arn:aws:dynamodb:ap-southeast-2:123:table/MyTable"]
        mock_config.return_value = cfg

        mock_dynamo = MagicMock()
        mock_dynamo.describe_continuous_backups.return_value = {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": "DISABLED",
                }
            }
        }
        mock_dynamo.list_exports.return_value = {"ExportSummaries": []}
        mock_boto.Session.return_value.client.return_value = mock_dynamo

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert result.exit_code == 1
        assert "PITR DISABLED" in result.output
        assert "no completed exports" in result.output

    @patch("aws_snapshot.commands.test.check_bucket_integrity")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_dynamodb_pitr_check_error(self, mock_config, mock_boto, mock_account, mock_integrity):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg, source_cfg = _make_integrity_mocks()
        source_cfg.s3_buckets = []
        source_cfg.dynamodb_tables = ["arn:aws:dynamodb:ap-southeast-2:123:table/MyTable"]
        mock_config.return_value = cfg

        mock_dynamo = MagicMock()
        mock_dynamo.describe_continuous_backups.side_effect = Exception("Denied")
        mock_dynamo.list_exports.side_effect = Exception("Denied")
        mock_boto.Session.return_value.client.return_value = mock_dynamo

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "mysource"])

        assert result.exit_code == 1
        assert "Could not check PITR" in result.output


# ---------------------------------------------------------------------------
# test_restore — direct copy flow, error paths
# ---------------------------------------------------------------------------


def _make_restore_mocks(inventory_mode=True):
    """Build standard mock config for restore tests."""
    source_cfg = MagicMock()
    source_cfg.s3_buckets = [MagicMock()]
    source_cfg.s3_buckets[0].arn = "arn:aws:s3:::src-bucket"
    source_cfg.s3_buckets[0].label = "src"
    source_cfg.get_backup_bucket_name.return_value = "backup-bucket"
    source_cfg.batch_manifest_mode = "inventory" if inventory_mode else "inline"
    source_cfg.source_account_id = None
    source_cfg.source_account_role_arn = None
    source_cfg.dynamodb_tables = []

    cfg = MagicMock()
    cfg.sources = {"mysource": source_cfg}
    cfg.general.region = "ap-southeast-2"
    cfg.general.s3_batch_role_arn = "arn:aws:iam::123:role/batch"
    return cfg


class TestRestoreDirectCopy:
    """Test restore command with direct copy path."""

    @patch("aws_snapshot.commands.test._delete_temp_bucket_silent", return_value=None)
    @patch("aws_snapshot.commands.test._verify_restored_object", return_value=None)
    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_direct_copy_success(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
        mock_verify,
        mock_cleanup,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks()
        mock_config.return_value = cfg

        sample = [
            {"Key": "file1.txt", "ETag": '"aaa"', "Size": 100},
            {"Key": "file2.txt", "ETag": '"bbb"', "Size": 200},
        ]

        mock_s3 = MagicMock()
        mock_boto.Session.return_value.client.return_value = mock_s3

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            return_value=sample,
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert "2 objects copied and verified" in result.output
        assert mock_s3.copy_object.call_count == 2
        mock_cleanup.assert_called_once()

    @patch("aws_snapshot.commands.test._delete_temp_bucket_silent", return_value=None)
    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_direct_copy_error(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
        mock_cleanup,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks()
        mock_config.return_value = cfg

        sample = [{"Key": "file1.txt", "ETag": '"aaa"', "Size": 100}]

        mock_s3 = MagicMock()
        mock_s3.copy_object.side_effect = Exception("CopyFailed")
        mock_boto.Session.return_value.client.return_value = mock_s3

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            return_value=sample,
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert result.exit_code == 1
        assert "copy error" in result.output.lower()
        mock_cleanup.assert_called_once()

    @patch("aws_snapshot.commands.test._delete_temp_bucket_silent", return_value=None)
    @patch(
        "aws_snapshot.commands.test._verify_restored_object",
        return_value="ETag mismatch: a != b",
    )
    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_direct_copy_etag_mismatch(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
        mock_verify,
        mock_cleanup,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks()
        mock_config.return_value = cfg

        sample = [{"Key": "file1.txt", "ETag": '"aaa"', "Size": 100}]

        mock_s3 = MagicMock()
        mock_boto.Session.return_value.client.return_value = mock_s3

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            return_value=sample,
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert result.exit_code == 1
        assert "mismatch" in result.output.lower()

    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_temp_bucket_creation_failure(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks()
        mock_config.return_value = cfg

        sample = [{"Key": "file1.txt", "ETag": '"aaa"', "Size": 100}]

        mock_s3 = MagicMock()
        mock_s3.create_bucket.side_effect = Exception("BucketFailed")
        mock_boto.Session.return_value.client.return_value = mock_s3

        with patch(
            "aws_snapshot.athena_inventory.sample_objects_via_inventory",
            return_value=sample,
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert result.exit_code == 1
        assert "Failed to create temp bucket" in result.output

    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_no_copyable_objects(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks(inventory_mode=False)
        mock_config.return_value = cfg

        mock_s3 = MagicMock()
        mock_s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
        mock_boto.Session.return_value.client.return_value = mock_s3

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert result.exit_code == 1
        assert "No copyable objects" in result.output


# ---------------------------------------------------------------------------
# test_restore — DynamoDB restorability checks
# ---------------------------------------------------------------------------


class TestRestoreDynamoDB:
    """Test restore command DynamoDB checks."""

    @patch("aws_snapshot.commands.test.append_event")
    @patch("aws_snapshot.commands.test.get_account_id", return_value="123456")
    @patch("aws_snapshot.commands.test.boto3")
    @patch("aws_snapshot.commands.test.load_config")
    def test_dynamodb_pitr_and_export_check(
        self,
        mock_config,
        mock_boto,
        mock_account,
        mock_event,
    ):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cfg = _make_restore_mocks()
        cfg.sources["mysource"].s3_buckets = []
        cfg.sources["mysource"].dynamodb_tables = [
            "arn:aws:dynamodb:ap-southeast-2:123:table/TestTable"
        ]
        cfg.sources["mysource"].get_dynamodb_backup_bucket_name.return_value = "bb-test-dynamo"
        mock_config.return_value = cfg

        mock_dynamo = MagicMock()
        mock_dynamo.describe_continuous_backups.return_value = {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": "ENABLED",
                    "LatestRestorableDateTime": "2026-05-01T00:00:00+00:00",
                }
            }
        }

        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {"KeyCount": 1}

        def client_factory(service, **kw):
            if service == "dynamodb":
                return mock_dynamo
            return mock_s3

        mock_boto.Session.return_value.client.side_effect = client_factory

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "mysource"])

        assert "PITR enabled" in result.output
        assert "export bucket accessible" in result.output


class TestRestoreTestResult:
    """Aggregation logic for the programmatic restore-test API."""

    def test_overall_skipped_when_no_buckets(self):
        from aws_snapshot.commands.test import RestoreTestResult

        result = RestoreTestResult(source="x", mode="direct copy")
        assert result.overall == "skipped"

    def test_overall_failed_when_any_bucket_failed(self):
        from aws_snapshot.commands.test import BucketRestoreResult, RestoreTestResult

        result = RestoreTestResult(source="x", mode="direct copy")
        result.buckets = [
            BucketRestoreResult(
                source_bucket="s1", backup_bucket="b1", result="passed", sample_count=10
            ),
            BucketRestoreResult(
                source_bucket="s2", backup_bucket="b2", result="failed", sample_count=0
            ),
        ]
        assert result.overall == "failed"

    def test_overall_passed_when_all_buckets_passed(self):
        from aws_snapshot.commands.test import BucketRestoreResult, RestoreTestResult

        result = RestoreTestResult(source="x", mode="direct copy")
        result.buckets = [
            BucketRestoreResult(
                source_bucket="s1", backup_bucket="b1", result="passed", sample_count=10
            ),
            BucketRestoreResult(
                source_bucket="s2", backup_bucket="b2", result="passed", sample_count=10
            ),
        ]
        assert result.overall == "passed"

    def test_overall_skipped_when_all_buckets_skipped(self):
        from aws_snapshot.commands.test import BucketRestoreResult, RestoreTestResult

        result = RestoreTestResult(source="x", mode="direct copy")
        result.buckets = [
            BucketRestoreResult(
                source_bucket="s1", backup_bucket="b1", result="skipped", sample_count=0
            ),
        ]
        assert result.overall == "skipped"


class TestTestAlert:
    """`backup test alert` exercises the CloudWatch alarm fast path."""

    @patch("aws_snapshot.commands.test.boto3")
    def test_default_stage_calls_set_alarm_state(self, mock_boto):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cw = MagicMock()
        mock_boto.client.return_value = cw

        runner = CliRunner()
        result = runner.invoke(app, ["alert"])

        assert result.exit_code == 0
        mock_boto.client.assert_called_once_with("cloudwatch", region_name="ap-southeast-2")
        cw.set_alarm_state.assert_called_once()
        kwargs = cw.set_alarm_state.call_args.kwargs
        assert kwargs["AlarmName"] == "nzshm-backup-lambda-errors-prod"
        assert kwargs["StateValue"] == "ALARM"
        assert "Manual test" in kwargs["StateReason"]

    @patch("aws_snapshot.commands.test.boto3")
    def test_custom_stage_and_region(self, mock_boto):
        from typer.testing import CliRunner

        from aws_snapshot.commands.test import app

        cw = MagicMock()
        mock_boto.client.return_value = cw

        runner = CliRunner()
        result = runner.invoke(app, ["alert", "--stage", "sandbox", "--region", "us-east-1"])

        assert result.exit_code == 0
        mock_boto.client.assert_called_once_with("cloudwatch", region_name="us-east-1")
        kwargs = cw.set_alarm_state.call_args.kwargs
        assert kwargs["AlarmName"] == "nzshm-backup-lambda-errors-sandbox"
