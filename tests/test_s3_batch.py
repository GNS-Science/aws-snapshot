"""Tests for S3 Batch Operations module."""

import boto3
import pytest
from moto import mock_aws

from aws_snapshot.s3_batch import (
    _build_restore_manifest_rows,
    batch_backup_source,
    batch_restore_bucket,
    build_manifest_csv,
    write_manifest_to_s3,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aws_session():
    with mock_aws():
        yield boto3.Session(region_name="ap-southeast-2")


@pytest.fixture
def s3_client(aws_session):
    return aws_session.client("s3")


def _create_bucket(s3_client, name):
    s3_client.create_bucket(
        Bucket=name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )


def _put_object(s3_client, bucket, key, body=b"data"):
    s3_client.put_object(Bucket=bucket, Key=key, Body=body)


def _head_etag(s3_client, bucket, key) -> str:
    return s3_client.head_object(Bucket=bucket, Key=key)["ETag"]


# ---------------------------------------------------------------------------
# build_manifest_csv
# ---------------------------------------------------------------------------


def test_manifest_all_new_objects(s3_client):
    """All source objects appear in manifest when backup is empty."""
    _create_bucket(s3_client, "src")
    _put_object(s3_client, "src", "a.txt", b"aaa")
    _put_object(s3_client, "src", "b.txt", b"bbb")

    source_objs = {
        "a.txt": {"Key": "a.txt", "ETag": '"etag1"', "Size": 3},
        "b.txt": {"Key": "b.txt", "ETag": '"etag2"', "Size": 3},
    }
    rows = list(build_manifest_csv(source_objs, {}, "src"))
    assert len(rows) == 2
    assert "src,a.txt\n" in rows
    assert "src,b.txt\n" in rows


def test_manifest_skips_unchanged(s3_client):
    """Objects with identical ETag and size are skipped."""
    source_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag1"', "Size": 3}}
    dest_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag1"', "Size": 3}}

    rows = list(build_manifest_csv(source_objs, dest_objs, "src"))
    assert rows == []


def test_manifest_includes_changed_etag():
    """Objects with different ETag are included."""
    source_objs = {"a.txt": {"Key": "a.txt", "ETag": '"new"', "Size": 3}}
    dest_objs = {"a.txt": {"Key": "a.txt", "ETag": '"old"', "Size": 3}}

    rows = list(build_manifest_csv(source_objs, dest_objs, "src"))
    assert len(rows) == 1


def test_manifest_includes_changed_size():
    """Objects with different size are included."""
    source_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag"', "Size": 10}}
    dest_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag"', "Size": 5}}

    rows = list(build_manifest_csv(source_objs, dest_objs, "src"))
    assert len(rows) == 1


def test_manifest_full_sync_copies_all():
    """full_sync=True copies all objects regardless of ETag match."""
    source_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag"', "Size": 3}}
    dest_objs = {"a.txt": {"Key": "a.txt", "ETag": '"etag"', "Size": 3}}

    rows = list(build_manifest_csv(source_objs, dest_objs, "src", full_sync=True))
    assert len(rows) == 1


def test_manifest_key_with_quotes():
    """Keys containing quotes are URL-encoded for S3 Batch manifests."""
    source_objs = {'say "hello".txt': {"Key": 'say "hello".txt', "ETag": '"e"', "Size": 1}}
    rows = list(build_manifest_csv(source_objs, {}, "src"))
    assert len(rows) == 1
    assert "say%20%22hello%22.txt" in rows[0]


def test_manifest_url_encodes_reserved_key_chars():
    """Reserved URL characters in keys are encoded (path separators preserved)."""
    source_objs = {
        "NZSHM22_AGG_RAW/vs30=1000/imt=SA(0.15)/_metadata.csv": {
            "Key": "NZSHM22_AGG_RAW/vs30=1000/imt=SA(0.15)/_metadata.csv",
            "ETag": '"e"',
            "Size": 1,
        }
    }
    rows = list(build_manifest_csv(source_objs, {}, "src"))
    assert len(rows) == 1
    assert "vs30%3D1000/imt%3DSA%280.15%29" in rows[0]


# ---------------------------------------------------------------------------
# write_manifest_to_s3
# ---------------------------------------------------------------------------


def test_write_manifest_to_s3(s3_client):
    """Manifest is written to S3 and row count is returned."""
    _create_bucket(s3_client, "backup")

    rows = iter(["bucket,key1\n", "bucket,key2\n", "bucket,key3\n"])
    etag, count = write_manifest_to_s3(s3_client, rows, "backup", "_manifests/test.csv")

    assert count == 3
    assert etag  # non-empty string

    obj = s3_client.get_object(Bucket="backup", Key="_manifests/test.csv")
    content = obj["Body"].read().decode()
    assert "bucket,key1\n" in content
    assert "bucket,key3\n" in content


def test_write_manifest_empty(s3_client):
    """Empty manifest (zero rows) writes an empty object."""
    _create_bucket(s3_client, "backup")
    etag, count = write_manifest_to_s3(s3_client, iter([]), "backup", "_manifests/empty.csv")
    assert count == 0
    assert etag


# ---------------------------------------------------------------------------
# batch_backup_source — dry run
# ---------------------------------------------------------------------------


def test_batch_backup_dry_run(aws_session, s3_client):
    """Dry run skips full object enumeration — just validates access and returns SKIPPED."""
    _create_bucket(s3_client, "src")
    _put_object(s3_client, "src", "file1.txt")
    _put_object(s3_client, "src", "file2.txt")

    result = batch_backup_source(
        session=aws_session,
        source_bucket="src",
        backup_bucket="src-backup-ap-southeast-2-123456789012",
        batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
        account_id="123456789012",
        dry_run=True,
    )

    assert result.status == "SKIPPED"
    assert result.dry_run is True
    assert result.objects_in_manifest == -1  # not enumerated in dry-run fast-path
    assert result.job_id is None


def test_batch_backup_dry_run_nothing_to_copy(aws_session, s3_client):
    """Dry run fast-path returns -1 regardless of source/backup state."""
    _create_bucket(s3_client, "src2")
    _put_object(s3_client, "src2", "unchanged.txt", b"data")
    backup_name = "src2-backup-ap-southeast-2-123456789012"
    _create_bucket(s3_client, backup_name)
    s3_client.copy_object(
        CopySource={"Bucket": "src2", "Key": "unchanged.txt"},
        Bucket=backup_name,
        Key="unchanged.txt",
    )

    result = batch_backup_source(
        session=aws_session,
        source_bucket="src2",
        backup_bucket=backup_name,
        batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
        account_id="123456789012",
        dry_run=True,
    )

    assert result.status == "SKIPPED"
    assert result.dry_run is True
    assert result.objects_in_manifest == -1


# ---------------------------------------------------------------------------
# batch_backup_source — live (mocked s3control)
# ---------------------------------------------------------------------------


def test_batch_backup_skipped_when_nothing_to_copy(aws_session, s3_client):
    """Returns SKIPPED when manifest is empty (no s3control call needed)."""
    _create_bucket(s3_client, "src3")
    _put_object(s3_client, "src3", "same.txt", b"data")
    backup_name = "src3-backup-ap-southeast-2-123456789012"

    # Tag backup bucket so ensure_backup_bucket_ready accepts it
    from aws_snapshot.s3_backup import create_backup_bucket

    create_backup_bucket(s3_client, backup_name, "ap-southeast-2", "123456789012")
    s3_client.copy_object(
        CopySource={"Bucket": "src3", "Key": "same.txt"},
        Bucket=backup_name,
        Key="same.txt",
    )

    result = batch_backup_source(
        session=aws_session,
        source_bucket="src3",
        backup_bucket=backup_name,
        batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
        account_id="123456789012",
        dry_run=False,
    )

    assert result.status == "SKIPPED"
    assert result.objects_in_manifest == 0
    assert result.job_id is None


def test_batch_backup_prepare_only_writes_manifest_without_submitting(aws_session, s3_client):
    """Prepare-only mode writes manifest and returns PREPARED without job submission."""
    _create_bucket(s3_client, "src4")
    _put_object(s3_client, "src4", "new.txt", b"data")
    backup_name = "src4-backup-ap-southeast-2-123456789012"

    from aws_snapshot.s3_backup import create_backup_bucket

    create_backup_bucket(s3_client, backup_name, "ap-southeast-2", "123456789012")

    result = batch_backup_source(
        session=aws_session,
        source_bucket="src4",
        backup_bucket=backup_name,
        batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
        account_id="123456789012",
        dry_run=False,
        prepare_only=True,
    )

    assert result.status == "PREPARED"
    assert result.job_id is None
    assert result.objects_in_manifest == 1
    manifest = s3_client.get_object(Bucket=backup_name, Key=result.manifest_key)
    assert manifest["Body"].read().decode().strip() == "src4,new.txt"


def test_batch_backup_inventory_mode_uses_inventory_builder(aws_session, s3_client):
    """Inventory mode uses inventory diff builder and avoids live bucket listing."""
    _create_bucket(s3_client, "src5")
    backup_name = "src5-backup-ap-southeast-2-123456789012"

    from aws_snapshot.s3_backup import create_backup_bucket

    create_backup_bucket(s3_client, backup_name, "ap-southeast-2", "123456789012")

    with pytest.MonkeyPatch.context() as mp:
        # _build_manifest_via_inventory now returns (etag, src_dt, bkp_dt, row_count)
        # and writes the manifest directly — no streaming through write_manifest_to_s3.
        mp.setattr(
            "aws_snapshot.s3_batch._build_manifest_via_inventory",
            lambda *a, **k: ('"test-etag"', "2026-04-23-00-00", "2026-04-23-00-00", 1),
        )
        mp.setattr(
            "aws_snapshot.s3_batch._list_bucket",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("_list_bucket should not be used")
            ),
        )
        mp.setattr(
            "aws_snapshot.s3_batch.write_manifest_to_s3",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("write_manifest_to_s3 should not be used in inventory mode")
            ),
        )

        result = batch_backup_source(
            session=aws_session,
            source_bucket="src5",
            backup_bucket=backup_name,
            batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
            account_id="123456789012",
            dry_run=False,
            prepare_only=True,
            source_alias="ths",
            manifest_mode="inventory",
        )

    assert result.status == "PREPARED"
    assert result.objects_in_manifest == 1


def test_batch_backup_inventory_mode_requires_source_alias(aws_session, s3_client):
    """Inventory mode requires source alias for inventory prefix resolution."""
    _create_bucket(s3_client, "src6")
    backup_name = "src6-backup-ap-southeast-2-123456789012"

    from aws_snapshot.s3_backup import create_backup_bucket

    create_backup_bucket(s3_client, backup_name, "ap-southeast-2", "123456789012")

    with pytest.raises(ValueError, match="source_alias"):
        batch_backup_source(
            session=aws_session,
            source_bucket="src6",
            backup_bucket=backup_name,
            batch_role_arn="arn:aws:iam::123456789012:role/nzshm-backup-batch-role",
            account_id="123456789012",
            dry_run=False,
            manifest_mode="inventory",
        )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_requires_batch_role_when_use_s3_batch():
    """ConfigModel raises if use_s3_batch=True but s3_batch_role_arn is missing."""
    import pytest
    from pydantic import ValidationError

    from aws_snapshot.config.models import ConfigModel

    with pytest.raises(ValidationError, match="s3_batch_role_arn"):
        ConfigModel(
            sources={
                "toshi": {
                    "display_name": "Toshi",
                    "s3_buckets": [{"arn": "arn:aws:s3:::my-bucket", "label": "main"}],
                    "use_s3_batch": True,
                }
            }
        )


# ---------------------------------------------------------------------------
# _build_restore_manifest_rows
# ---------------------------------------------------------------------------


def test_build_restore_manifest_rows_excludes_operational(s3_client):
    """Operational prefix objects are excluded from restore manifest rows."""
    _create_bucket(s3_client, "bb-src-s3-main-ap-southeast-2-111111111111")
    bucket = "bb-src-s3-main-ap-southeast-2-111111111111"
    _put_object(s3_client, bucket, "data/file.txt")
    _put_object(s3_client, bucket, "_manifests/m.csv")
    _put_object(s3_client, bucket, "_batch-reports/r.csv")
    _put_object(s3_client, bucket, "_state/last-run.json")

    rows = list(_build_restore_manifest_rows(s3_client, bucket))

    assert len(rows) == 1
    assert f"{bucket},data/file.txt\n" in rows


def test_build_restore_manifest_rows_prefix_filter(s3_client):
    """Only objects under the given prefix are yielded."""
    _create_bucket(s3_client, "bb-src2")
    _put_object(s3_client, "bb-src2", "a/file1.txt")
    _put_object(s3_client, "bb-src2", "b/file2.txt")

    rows = list(_build_restore_manifest_rows(s3_client, "bb-src2", prefix="a/"))

    assert len(rows) == 1
    assert "bb-src2,a/file1.txt\n" in rows


# ---------------------------------------------------------------------------
# batch_restore_bucket
# ---------------------------------------------------------------------------


def test_batch_restore_dry_run(aws_session, s3_client):
    """Dry run counts restorable objects without writing manifest or submitting job."""
    backup = "bb-src-s3-main-ap-southeast-2-111111111111"
    _create_bucket(s3_client, backup)
    _put_object(s3_client, backup, "data/a.txt")
    _put_object(s3_client, backup, "data/b.txt")
    _put_object(s3_client, backup, "_manifests/m.csv")  # excluded

    result = batch_restore_bucket(
        session=aws_session,
        backup_bucket=backup,
        target_bucket="original-bucket",
        batch_role_arn="arn:aws:iam::111111111111:role/nzshm-backup-batch-role",
        account_id="111111111111",
        dry_run=True,
    )

    assert result.status == "SKIPPED"
    assert result.dry_run is True
    assert result.objects_in_manifest == 2
    assert result.job_id is None


def test_batch_restore_skipped_when_empty(aws_session, s3_client):
    """Returns SKIPPED when backup bucket is empty (nothing to restore)."""
    backup = "bb-empty-s3-main-ap-southeast-2-111111111111"
    _create_bucket(s3_client, backup)

    result = batch_restore_bucket(
        session=aws_session,
        backup_bucket=backup,
        target_bucket="original-bucket",
        batch_role_arn="arn:aws:iam::111111111111:role/nzshm-backup-batch-role",
        account_id="111111111111",
        dry_run=False,
    )

    assert result.status == "SKIPPED"
    assert result.objects_in_manifest == 0
    assert result.job_id is None


def test_batch_restore_excludes_operational_in_dry_run(aws_session, s3_client):
    """Operational prefixes are excluded even in dry_run mode."""
    backup = "bb-ops-s3-main-ap-southeast-2-111111111111"
    _create_bucket(s3_client, backup)
    _put_object(s3_client, backup, "real/data.json")
    _put_object(s3_client, backup, "_state/last-run.json")
    _put_object(s3_client, backup, "_batch-reports/job.csv")

    result = batch_restore_bucket(
        session=aws_session,
        backup_bucket=backup,
        target_bucket="dest",
        batch_role_arn="arn:aws:iam::111111111111:role/nzshm-backup-batch-role",
        account_id="111111111111",
        dry_run=True,
    )

    assert result.objects_in_manifest == 1


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_accepts_batch_role_when_use_s3_batch():
    """ConfigModel validates when s3_batch_role_arn is provided."""
    from aws_snapshot.config.models import ConfigModel

    cfg = ConfigModel(
        general={"s3_batch_role_arn": "arn:aws:iam::123456789012:role/nzshm-backup-batch-role"},
        sources={
            "toshi": {
                "display_name": "Toshi",
                "s3_buckets": [{"arn": "arn:aws:s3:::my-bucket", "label": "main"}],
                "use_s3_batch": True,
            }
        },
    )
    assert cfg.general.s3_batch_role_arn.startswith("arn:aws:iam::")
    assert cfg.sources["toshi"].use_s3_batch is True


# ---------------------------------------------------------------------------
# write_manifest_to_s3 — progress logging and error handling
# ---------------------------------------------------------------------------


@mock_aws
def test_write_manifest_progress_logging(aws_session, s3_client, caplog):
    """Progress is logged every 100K rows."""
    import logging

    _create_bucket(s3_client, "manifest-bucket")

    # Generate >100K rows to trigger progress log
    def _rows():
        for i in range(100_001):
            yield f"bucket,key{i}\n"

    with caplog.at_level(logging.INFO, logger="aws_snapshot.s3_batch"):
        etag, count = write_manifest_to_s3(
            s3_client,
            _rows(),
            "manifest-bucket",
            "test.csv",
            result_bytes=5_000_000,
        )

    assert count == 100_001
    assert any("Manifest progress: 100,000 rows" in msg for msg in caplog.messages)
    assert any("est. rows" in msg for msg in caplog.messages)
    assert any("Lambda timeout" in msg for msg in caplog.messages)


@mock_aws
def test_write_manifest_progress_unknown_total(aws_session, s3_client, caplog):
    """Progress log shows 'total unknown' when result_bytes=0."""
    import logging

    _create_bucket(s3_client, "manifest-bucket2")

    def _rows():
        for i in range(100_001):
            yield f"bucket,key{i}\n"

    with caplog.at_level(logging.INFO, logger="aws_snapshot.s3_batch"):
        write_manifest_to_s3(s3_client, _rows(), "manifest-bucket2", "t.csv")

    assert any("total unknown" in msg for msg in caplog.messages)


@mock_aws
def test_write_manifest_abort_on_error(aws_session, s3_client):
    """Manifest upload aborts multipart on error."""
    from unittest.mock import MagicMock

    mock_s3 = MagicMock()
    mock_s3.create_multipart_upload.return_value = {"UploadId": "up-err"}
    mock_s3.upload_part.side_effect = Exception("UploadFailed")

    with pytest.raises(Exception, match="UploadFailed"):
        write_manifest_to_s3(mock_s3, iter(["row\n"]), "bkt", "key.csv")

    mock_s3.abort_multipart_upload.assert_called_once_with(
        Bucket="bkt",
        Key="key.csv",
        UploadId="up-err",
    )


# ---------------------------------------------------------------------------
# wait_for_batch_job
# ---------------------------------------------------------------------------


def test_wait_for_batch_job_completes():
    """Returns terminal status when job completes."""
    from unittest.mock import MagicMock, patch

    from aws_snapshot.s3_batch import wait_for_batch_job

    mock_session = MagicMock()
    mock_s3ctrl = MagicMock()
    mock_session.client.return_value = mock_s3ctrl
    mock_s3ctrl.describe_job.return_value = {"Job": {"Status": "Complete"}}

    with patch("aws_snapshot.s3_batch.get_region", return_value="ap-southeast-2"):
        with patch("aws_snapshot.s3_batch.time.sleep"):
            status = wait_for_batch_job(mock_session, "123", "job-1", timeout=60)

    assert status == "Complete"


def test_wait_for_batch_job_polls_then_completes():
    """Polls Active then returns Complete."""
    from unittest.mock import MagicMock, patch

    from aws_snapshot.s3_batch import wait_for_batch_job

    mock_session = MagicMock()
    mock_s3ctrl = MagicMock()
    mock_session.client.return_value = mock_s3ctrl
    mock_s3ctrl.describe_job.side_effect = [
        {"Job": {"Status": "Active"}},
        {"Job": {"Status": "Active"}},
        {"Job": {"Status": "Complete"}},
    ]

    with patch("aws_snapshot.s3_batch.get_region", return_value="ap-southeast-2"):
        with patch("aws_snapshot.s3_batch.time.sleep"):
            status = wait_for_batch_job(
                mock_session,
                "123",
                "job-2",
                poll_interval=1,
                timeout=60,
            )

    assert status == "Complete"
    assert mock_s3ctrl.describe_job.call_count == 3


def test_wait_for_batch_job_returns_failed():
    """Returns Failed status."""
    from unittest.mock import MagicMock, patch

    from aws_snapshot.s3_batch import wait_for_batch_job

    mock_session = MagicMock()
    mock_s3ctrl = MagicMock()
    mock_session.client.return_value = mock_s3ctrl
    mock_s3ctrl.describe_job.return_value = {"Job": {"Status": "Failed"}}

    with patch("aws_snapshot.s3_batch.get_region", return_value="ap-southeast-2"):
        with patch("aws_snapshot.s3_batch.time.sleep"):
            status = wait_for_batch_job(mock_session, "123", "job-3")

    assert status == "Failed"


def test_wait_for_batch_job_timeout():
    """Raises TimeoutError when job doesn't complete."""
    from unittest.mock import MagicMock, patch

    from aws_snapshot.s3_batch import wait_for_batch_job

    mock_session = MagicMock()
    mock_s3ctrl = MagicMock()
    mock_session.client.return_value = mock_s3ctrl
    mock_s3ctrl.describe_job.return_value = {"Job": {"Status": "Active"}}

    with patch("aws_snapshot.s3_batch.get_region", return_value="ap-southeast-2"):
        with patch("aws_snapshot.s3_batch.time.sleep"):
            with pytest.raises(TimeoutError, match="did not complete"):
                wait_for_batch_job(
                    mock_session,
                    "123",
                    "job-4",
                    poll_interval=1,
                    timeout=3,
                )


# ---------------------------------------------------------------------------
# batch_backup_source — CreateJob success and failure
# ---------------------------------------------------------------------------


@mock_aws
def test_batch_backup_inline_createjob_failure(aws_session, s3_client):
    """CreateJob ClientError returns FAILED result instead of raising."""
    from unittest.mock import MagicMock

    _create_bucket(s3_client, "src-cj")
    s3_client.put_object(Bucket="src-cj", Key="a.txt", Body=b"data")

    backup_name = "src-cj-backup-ap-southeast-2-123456789012"
    from aws_snapshot.s3_backup import create_backup_bucket

    create_backup_bucket(s3_client, backup_name, "ap-southeast-2", "123456789012")

    # Mock s3control to raise ClientError
    mock_s3ctrl = MagicMock()
    from botocore.exceptions import ClientError

    mock_s3ctrl.create_job.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
        "CreateJob",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "aws_snapshot.s3_batch.get_region",
            lambda s: "ap-southeast-2",
        )
        original_client = aws_session.client

        def patched_client(service, **kw):
            if service == "s3control":
                return mock_s3ctrl
            return original_client(service, **kw)

        mp.setattr(aws_session, "client", patched_client)

        result = batch_backup_source(
            session=aws_session,
            source_bucket="src-cj",
            backup_bucket=backup_name,
            batch_role_arn="arn:aws:iam::123456789012:role/batch",
            account_id="123456789012",
            dry_run=False,
        )

    assert result.status == "FAILED"
    assert len(result.errors) > 0
