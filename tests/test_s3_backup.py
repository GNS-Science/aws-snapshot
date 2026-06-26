"""Tests for S3 backup operations using moto mocks."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_snapshot.s3_backup import (
    apply_lifecycle_policy,
    backup_source,
    bucket_exists,
    bucket_is_ours,
    create_backup_bucket,
    enable_versioning,
    sync_bucket,
)


@pytest.fixture
def s3_client():
    """Create mocked S3 client."""
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        yield client


@pytest.fixture
def sts_client():
    """Create mocked STS client."""
    with mock_aws():
        client = boto3.client("sts", region_name="ap-southeast-2")
        yield client


@pytest.fixture
def source_bucket(s3_client):
    """Create source bucket with test objects."""
    bucket_name = "test-source-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    s3_client.put_object(Bucket=bucket_name, Key="file1.txt", Body=b"content1")
    s3_client.put_object(Bucket=bucket_name, Key="file2.txt", Body=b"content2")
    s3_client.put_object(Bucket=bucket_name, Key="folder/file3.txt", Body=b"content3")

    return bucket_name


def test_bucket_exists_true(s3_client, source_bucket):
    """Test bucket_exists returns True for existing bucket."""
    assert bucket_exists(s3_client, source_bucket) is True


def test_bucket_exists_false(s3_client):
    """Test bucket_exists returns False for non-existent bucket."""
    assert bucket_exists(s3_client, "nonexistent-bucket") is False


def test_create_backup_bucket(s3_client):
    """Test backup bucket creation."""
    bucket_name = "test-backup-bucket"

    create_backup_bucket(s3_client, bucket_name, "ap-southeast-2", "123456789012")

    assert bucket_exists(s3_client, bucket_name) is True

    tags = s3_client.get_bucket_tagging(Bucket=bucket_name)
    tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}
    assert tag_dict["ManagedBy"] == "nzshm-backup"
    assert tag_dict["Type"] == "backup"


def test_create_backup_bucket_already_exists(s3_client):
    """Test creating bucket that already exists raises error."""
    bucket_name = "test-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    with pytest.raises(ValueError, match="already exists"):
        create_backup_bucket(s3_client, bucket_name, "ap-southeast-2", "123456789012")


def test_bucket_is_ours_true(s3_client):
    """bucket_is_ours returns True for a bucket tagged ManagedBy: nzshm-backup."""
    bucket_name = "test-managed-bucket"
    create_backup_bucket(s3_client, bucket_name, "ap-southeast-2", "123456789012")
    assert bucket_is_ours(s3_client, bucket_name) is True


def test_bucket_is_ours_false_untagged(s3_client):
    """bucket_is_ours returns False for an untagged bucket."""
    bucket_name = "test-foreign-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )
    assert bucket_is_ours(s3_client, bucket_name) is False


def test_ensure_backup_bucket_ready_existing_ours(s3_client):
    """ensure_backup_bucket_ready proceeds without error if bucket is ours."""
    from aws_snapshot.s3_backup import ensure_backup_bucket_ready

    bucket_name = "test-managed-bucket"
    create_backup_bucket(s3_client, bucket_name, "ap-southeast-2", "123456789012")

    session = boto3.Session(region_name="ap-southeast-2")
    # Should not raise
    ensure_backup_bucket_ready(session, bucket_name)


def test_ensure_backup_bucket_ready_existing_foreign(s3_client):
    """ensure_backup_bucket_ready raises if bucket exists but is not ours."""
    from aws_snapshot.s3_backup import ensure_backup_bucket_ready

    bucket_name = "test-foreign-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    session = boto3.Session(region_name="ap-southeast-2")
    with pytest.raises(ValueError, match="not managed by nzshm-backup"):
        ensure_backup_bucket_ready(session, bucket_name)


def test_apply_lifecycle_policy(s3_client):
    """Test lifecycle policy application (ADR-006: single GLACIER_IR transition, no expiry)."""
    bucket_name = "test-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    apply_lifecycle_policy(s3_client, bucket_name)

    lifecycle = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
    assert len(lifecycle["Rules"]) == 1
    rule = lifecycle["Rules"][0]
    assert rule["ID"] == "BackupTierTransition"

    transitions = rule["Transitions"]
    assert len(transitions) == 1
    assert transitions[0]["Days"] == 30
    assert transitions[0]["StorageClass"] == "GLACIER_IR"
    assert "Expiration" not in rule


def test_sync_bucket_incremental(s3_client, source_bucket):
    """Test incremental sync copies only new objects."""
    backup_bucket = "test-backup-bucket"
    s3_client.create_bucket(
        Bucket=backup_bucket,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    result = sync_bucket(s3_client, source_bucket, backup_bucket)

    assert result.objects_copied == 3
    assert result.bytes_transferred > 0
    assert result.success is True

    for key in ["file1.txt", "file2.txt", "folder/file3.txt"]:
        response = s3_client.head_object(Bucket=backup_bucket, Key=key)
        assert response["ContentLength"] > 0


def test_sync_bucket_dry_run(s3_client, source_bucket):
    """Test dry run sync doesn't copy objects."""
    from botocore.exceptions import ClientError

    backup_bucket = "test-backup-bucket"
    s3_client.create_bucket(
        Bucket=backup_bucket,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    result = sync_bucket(s3_client, source_bucket, backup_bucket, dry_run=True)

    assert result.objects_copied == 3
    assert result.dry_run is True

    with pytest.raises(ClientError) as exc_info:
        s3_client.head_object(Bucket=backup_bucket, Key="file1.txt")
    assert exc_info.value.response["Error"]["Code"] == "404"


def test_sync_bucket_skip_unchanged(s3_client, source_bucket):
    """Test sync skips unchanged objects."""
    backup_bucket = "test-backup-bucket"
    s3_client.create_bucket(
        Bucket=backup_bucket,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    s3_client.copy_object(
        CopySource={"Bucket": source_bucket, "Key": "file1.txt"},
        Bucket=backup_bucket,
        Key="file1.txt",
    )

    result = sync_bucket(s3_client, source_bucket, backup_bucket)

    assert result.objects_copied == 2
    assert result.objects_skipped == 1


def test_backup_source(s3_client, source_bucket):
    """Test full backup_source function."""
    backup_bucket = "test-source-bucket-backup-ap-southeast-2-123456789012"

    session = boto3.Session(region_name="ap-southeast-2")

    result = backup_source(
        session=session,
        source_bucket=source_bucket,
        backup_bucket_name=backup_bucket,
        dry_run=False,
    )

    assert result.objects_copied == 3
    assert bucket_exists(s3_client, backup_bucket) is True


def test_enable_versioning(s3_client):
    """enable_versioning turns on versioning for a bucket."""
    bucket_name = "test-versioned-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    enable_versioning(s3_client, bucket_name)

    resp = s3_client.get_bucket_versioning(Bucket=bucket_name)
    assert resp.get("Status") == "Enabled"


def test_apply_lifecycle_policy_includes_noncurrent_expiration(s3_client):
    """Lifecycle rule includes NoncurrentVersionExpiration when version_retention_days > 0."""
    from aws_snapshot.s3_backup import LifecycleConfig

    bucket_name = "test-versioned-lifecycle-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    apply_lifecycle_policy(s3_client, bucket_name, LifecycleConfig(version_retention_days=365))

    lifecycle = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
    rule = lifecycle["Rules"][0]
    assert "NoncurrentVersionExpiration" in rule
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 365


def test_apply_lifecycle_policy_no_noncurrent_expiration_when_zero(s3_client):
    """NoncurrentVersionExpiration is omitted when version_retention_days=0 (retain forever)."""
    from aws_snapshot.s3_backup import LifecycleConfig

    bucket_name = "test-no-expiry-bucket"
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )

    apply_lifecycle_policy(s3_client, bucket_name, LifecycleConfig(version_retention_days=0))

    lifecycle = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
    rule = lifecycle["Rules"][0]
    assert "NoncurrentVersionExpiration" not in rule


def test_ensure_backup_bucket_ready_enables_versioning(s3_client):
    """Newly created backup bucket has versioning enabled."""
    from aws_snapshot.s3_backup import ensure_backup_bucket_ready

    bucket_name = "test-new-backup-bucket"
    session = boto3.Session(region_name="ap-southeast-2")
    ensure_backup_bucket_ready(session, bucket_name)

    resp = s3_client.get_bucket_versioning(Bucket=bucket_name)
    assert resp.get("Status") == "Enabled"


def test_ensure_backup_bucket_ready_versioning_access_denied_has_remediation(s3_client):
    """AccessDenied on PutBucketVersioning raises a remediation-focused error."""
    from unittest.mock import patch

    from aws_snapshot.s3_backup import ensure_backup_bucket_ready

    bucket_name = "test-new-backup-bucket-perm"
    session = boto3.Session(region_name="ap-southeast-2")
    s3 = session.client("s3")
    sts = session.client("sts")
    err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "PutBucketVersioning",
    )

    with patch.object(s3, "put_bucket_versioning", side_effect=err):
        with patch.object(
            session,
            "client",
            side_effect=lambda svc, **kw: sts if svc == "sts" else s3,
        ):
            with pytest.raises(RuntimeError, match="missing s3:PutBucketVersioning permission"):
                ensure_backup_bucket_ready(session, bucket_name)
