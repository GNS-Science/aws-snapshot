"""Tests for S3 restore operations using moto mocks."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from nzshm_backup.s3_restore import RestoreResult, restore_s3_bucket

REGION = "ap-southeast-2"


def _make_bucket(s3_client, name: str) -> str:
    s3_client.create_bucket(
        Bucket=name,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    return name


@mock_aws
def test_restore_copies_all_objects():
    """Objects in backup bucket are copied to the target."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    s3.put_object(Bucket="backup-bucket", Key="a.txt", Body=b"hello")
    s3.put_object(Bucket="backup-bucket", Key="b.txt", Body=b"world")

    result = restore_s3_bucket(session, "backup-bucket", "target-bucket")

    assert result.success
    assert result.objects_copied == 2
    assert result.bytes_transferred > 0
    assert result.objects_skipped == 0
    # Target bucket was created automatically
    s3.head_bucket(Bucket="target-bucket")
    s3.head_object(Bucket="target-bucket", Key="a.txt")
    s3.head_object(Bucket="target-bucket", Key="b.txt")


@mock_aws
def test_restore_to_existing_target_bucket():
    """Restore proceeds without error if the target bucket already exists."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    _make_bucket(s3, "existing-target")
    s3.put_object(Bucket="backup-bucket", Key="file.txt", Body=b"data")

    result = restore_s3_bucket(session, "backup-bucket", "existing-target")

    assert result.success
    assert result.objects_copied == 1


@mock_aws
def test_restore_skips_already_present_matching_etag():
    """Objects already in target with matching ETag are skipped."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    _make_bucket(s3, "target-bucket")
    body = b"unchanged content"
    s3.put_object(Bucket="backup-bucket", Key="file.txt", Body=body)
    # Pre-populate target with the same content (same ETag)
    s3.copy_object(
        CopySource={"Bucket": "backup-bucket", "Key": "file.txt"},
        Bucket="target-bucket",
        Key="file.txt",
    )

    result = restore_s3_bucket(session, "backup-bucket", "target-bucket")

    assert result.objects_copied == 0
    assert result.objects_skipped == 1


@mock_aws
def test_restore_overwrites_different_etag():
    """Objects present in target but with a different ETag (changed content) are overwritten."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    _make_bucket(s3, "target-bucket")
    s3.put_object(Bucket="backup-bucket", Key="file.txt", Body=b"new content")
    s3.put_object(Bucket="target-bucket", Key="file.txt", Body=b"old content")

    result = restore_s3_bucket(session, "backup-bucket", "target-bucket")

    assert result.objects_copied == 1
    assert result.objects_skipped == 0
    body = s3.get_object(Bucket="target-bucket", Key="file.txt")["Body"].read()
    assert body == b"new content"


@mock_aws
def test_restore_with_prefix():
    """Only objects matching the prefix are restored."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    s3.put_object(Bucket="backup-bucket", Key="data/run1.h5", Body=b"r1")
    s3.put_object(Bucket="backup-bucket", Key="data/run2.h5", Body=b"r2")
    s3.put_object(Bucket="backup-bucket", Key="logs/run1.log", Body=b"log")

    result = restore_s3_bucket(session, "backup-bucket", "target-bucket", prefix="data/")

    assert result.objects_copied == 2
    # log file must NOT be present in target
    with pytest.raises(ClientError) as exc_info:
        s3.head_object(Bucket="target-bucket", Key="logs/run1.log")
    assert exc_info.value.response["Error"]["Code"] == "404"


@mock_aws
def test_restore_result_target_created_with_restored_by_tag():
    """New target bucket is tagged RestoredBy: nzshm-backup."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "backup-bucket")
    s3.put_object(Bucket="backup-bucket", Key="x.txt", Body=b"x")

    restore_s3_bucket(session, "backup-bucket", "fresh-target")

    tags = s3.get_bucket_tagging(Bucket="fresh-target")
    tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}
    assert tag_dict.get("RestoredBy") == "nzshm-backup"


@mock_aws
def test_restore_empty_backup_bucket():
    """Restoring an empty bucket returns success with zero objects."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "empty-backup")

    result = restore_s3_bucket(session, "empty-backup", "target-bucket")

    assert result.success
    assert result.objects_copied == 0
    assert isinstance(result, RestoreResult)
