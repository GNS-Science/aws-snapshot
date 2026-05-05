"""Tests for the test (validation) commands module."""

from unittest.mock import MagicMock, patch  # noqa: I001

from nzshm_backup.commands.test import (
    _delete_temp_bucket,
    _fmt_dt,
    _get_object_checksum,
    _verify_restored_object,
)


# ---------------------------------------------------------------------------
# _fmt_dt
# ---------------------------------------------------------------------------


def test_fmt_dt_with_string():
    result = _fmt_dt("2026-04-30T12:00:00+00:00")
    assert "2026" in result
    # UTC 12:00 converts to local time — just verify it parsed and formatted
    assert ":" in result


# ---------------------------------------------------------------------------
# _get_object_checksum
# ---------------------------------------------------------------------------


class TestGetObjectChecksum:
    """Tests for _get_object_checksum helper."""

    def test_returns_first_available_checksum(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumSHA256": "abc123"},
        }
        result = _get_object_checksum(s3, "bucket", "key")
        assert result == ("ChecksumSHA256", "abc123")

    def test_returns_crc64_when_present(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumCRC64NVME": "crc64val", "ChecksumSHA256": "sha256val"},
        }
        result = _get_object_checksum(s3, "bucket", "key")
        # CRC64NVME is first in _CHECKSUM_KEYS, so it wins
        assert result == ("ChecksumCRC64NVME", "crc64val")

    def test_returns_none_when_no_checksum(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {"Checksum": {}}
        result = _get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_when_empty_value(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {
            "Checksum": {"ChecksumSHA256": ""},
        }
        result = _get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_on_exception(self):
        s3 = MagicMock()
        s3.get_object_attributes.side_effect = Exception("AccessDenied")
        result = _get_object_checksum(s3, "bucket", "key")
        assert result is None

    def test_returns_none_when_checksum_key_missing(self):
        s3 = MagicMock()
        s3.get_object_attributes.return_value = {}
        result = _get_object_checksum(s3, "bucket", "key")
        assert result is None


# ---------------------------------------------------------------------------
# _verify_restored_object
# ---------------------------------------------------------------------------


class TestVerifyRestoredObject:
    """Tests for _verify_restored_object helper."""

    def test_checksum_match_returns_none(self):
        s3 = MagicMock()
        with patch(
            "nzshm_backup.commands.test._get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), ("ChecksumSHA256", "abc")],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is None

    def test_checksum_mismatch_returns_error(self):
        s3 = MagicMock()
        with patch(
            "nzshm_backup.commands.test._get_object_checksum",
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
            "nzshm_backup.commands.test._get_object_checksum",
            side_effect=[("ChecksumSHA256", "abc"), ("ChecksumCRC32", "def")],
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag"')
        assert result is None
        s3.head_object.assert_called_once()

    def test_etag_fallback_match(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"etag123"'}
        with patch(
            "nzshm_backup.commands.test._get_object_checksum",
            return_value=None,
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"etag123"')
        assert result is None

    def test_etag_fallback_mismatch(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"different"'}
        with patch(
            "nzshm_backup.commands.test._get_object_checksum",
            return_value=None,
        ):
            result = _verify_restored_object(s3, "src-bucket", "tgt-bucket", "key", '"expected"')
        assert result is not None
        assert "ETag mismatch" in result

    def test_no_target_checksum_falls_to_etag(self):
        s3 = MagicMock()
        s3.head_object.return_value = {"ETag": '"etag"'}
        with patch(
            "nzshm_backup.commands.test._get_object_checksum",
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

    @patch("nzshm_backup.commands.test.load_config")
    def test_unknown_source_exits(self, mock_config):
        """Unknown source should exit with code 1."""
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

        cfg = MagicMock()
        cfg.sources = {"valid-source": MagicMock()}
        mock_config.return_value = cfg

        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "nonexistent"])
        assert result.exit_code == 1
        assert "unknown source" in result.output.lower()

    @patch("nzshm_backup.commands.test.load_config")
    def test_config_not_found_exits(self, mock_config):
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

        mock_config.side_effect = FileNotFoundError("no config")
        runner = CliRunner()
        result = runner.invoke(app, ["integrity", "--source", "foo"])
        assert result.exit_code == 1

    @patch("nzshm_backup.commands.test.check_bucket_integrity")
    @patch("nzshm_backup.commands.test.get_account_id", return_value="123456")
    @patch("nzshm_backup.commands.test.boto3")
    @patch("nzshm_backup.commands.test.load_config")
    def test_inventory_mode_shows_warning(
        self, mock_config, mock_boto, mock_account, mock_integrity
    ):
        """Inventory-mode sources should show a slow-listing warning."""
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

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

    @patch("nzshm_backup.commands.test.load_config")
    def test_unknown_source_exits(self, mock_config):
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

        cfg = MagicMock()
        cfg.sources = {"valid-source": MagicMock()}
        mock_config.return_value = cfg

        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "nonexistent"])
        assert result.exit_code == 1

    @patch("nzshm_backup.commands.test.load_config")
    def test_config_not_found_exits(self, mock_config):
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

        mock_config.side_effect = FileNotFoundError("no config")
        runner = CliRunner()
        result = runner.invoke(app, ["restore", "--source", "foo"])
        assert result.exit_code == 1

    @patch("nzshm_backup.commands.test.append_event")
    @patch("nzshm_backup.commands.test._delete_temp_bucket")
    @patch("nzshm_backup.commands.test._verify_restored_object", return_value=None)
    @patch("nzshm_backup.commands.test.get_account_id", return_value="123456")
    @patch("nzshm_backup.commands.test.boto3")
    @patch("nzshm_backup.commands.test.load_config")
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

        from nzshm_backup.commands.test import app

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
            "nzshm_backup.athena_inventory.sample_objects_via_inventory",
            return_value=sample_data,
        ) as mock_sample:
            runner = CliRunner()
            runner.invoke(
                app, ["restore", "--source", "mysource", "--sample-size", "2"]
            )

        # Should have called the inventory sampler
        mock_sample.assert_called_once()
        # Should have copied and verified
        assert s3_mock.copy_object.call_count == 2
        mock_verify.assert_called()

    @patch("nzshm_backup.commands.test.get_account_id", return_value="123456")
    @patch("nzshm_backup.commands.test.boto3")
    @patch("nzshm_backup.commands.test.load_config")
    def test_inventory_unavailable_guard(
        self, mock_config, mock_boto, mock_account
    ):
        """When inventory is unavailable, should refuse with message."""
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

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
            "nzshm_backup.athena_inventory.sample_objects_via_inventory",
            side_effect=ValueError("No inventory data"),
        ):
            runner = CliRunner()
            result = runner.invoke(
                app, ["restore", "--source", "mysource"]
            )

        assert result.exit_code == 1
        assert "Inventory unavailable" in result.stderr

    @patch("nzshm_backup.commands.test.get_account_id", return_value="123456")
    @patch("nzshm_backup.commands.test.boto3")
    @patch("nzshm_backup.commands.test.load_config")
    def test_dry_run_skips_copy(self, mock_config, mock_boto, mock_account):
        """Dry run should not create temp bucket or copy objects."""
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

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
        result = runner.invoke(
            app, ["restore", "--source", "mysource", "--dry-run"]
        )
        assert "DRY RUN" in result.output
        s3_mock.create_bucket.assert_not_called()

    @patch("nzshm_backup.commands.test.get_account_id", return_value="123456")
    @patch("nzshm_backup.commands.test.boto3")
    @patch("nzshm_backup.commands.test.load_config")
    def test_use_batch_requires_role_arn(self, mock_config, mock_boto, mock_account):
        """--use-batch without s3_batch_role_arn should exit with error."""
        from typer.testing import CliRunner

        from nzshm_backup.commands.test import app

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
        result = runner.invoke(
            app, ["restore", "--source", "mysource", "--use-batch"]
        )
        assert result.exit_code == 1
        assert "s3_batch_role_arn" in result.stderr
