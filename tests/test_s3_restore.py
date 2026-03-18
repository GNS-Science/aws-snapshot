"""Tests for S3 restore operations using moto mocks."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from nzshm_backup.s3_restore import (
    RestoreResult,
    apply_restore_target_policy,
    make_restore_bucket_name,
    restore_s3_bucket,
)

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


# ---------------------------------------------------------------------------
# make_restore_bucket_name
# ---------------------------------------------------------------------------

def test_make_restore_bucket_name_short():
    """Short names get -restore appended as-is."""
    assert make_restore_bucket_name("mybucket") == "mybucket-restore"


def test_make_restore_bucket_name_exact_55_chars():
    """A 55-char base name fits exactly (55 + 8 = 63)."""
    base = "a" * 55
    assert make_restore_bucket_name(base) == base + "-restore"
    assert len(make_restore_bucket_name(base)) == 63


def test_make_restore_bucket_name_truncates_long_name():
    """Names longer than 55 chars are truncated so the result is 63 chars."""
    long_name = "arkivalist-api-dev-serverlessdeploymentbucket-oztlskap4vrh"  # 58 chars
    result = make_restore_bucket_name(long_name)
    assert result.endswith("-restore")
    assert len(result) == 63
    assert result == long_name[:55] + "-restore"


def test_make_restore_bucket_name_exactly_63_chars_result():
    """Result is always at most 63 characters."""
    for length in range(50, 70):
        result = make_restore_bucket_name("x" * length)
        assert len(result) <= 63
        assert result.endswith("-restore")


# ---------------------------------------------------------------------------
# apply_restore_target_policy
# ---------------------------------------------------------------------------

@mock_aws
def test_apply_restore_target_policy_creates_new_policy():
    """Policy is created on a bucket with no existing policy."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "restore-target")

    apply_restore_target_policy(s3, "restore-target", "arn:aws:iam::123456789012:role/batch-role")

    import json
    policy = json.loads(s3.get_bucket_policy(Bucket="restore-target")["Policy"])
    sids = [s["Sid"] for s in policy["Statement"]]
    assert "AllowNzshmBatchRoleWrite" in sids


@mock_aws
def test_apply_restore_target_policy_is_merge_safe():
    """Re-applying the policy replaces the existing SID, not duplicates it."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    _make_bucket(s3, "restore-target")
    batch_arn = "arn:aws:iam::123456789012:role/batch-role"

    apply_restore_target_policy(s3, "restore-target", batch_arn)
    apply_restore_target_policy(s3, "restore-target", batch_arn)

    import json
    policy = json.loads(s3.get_bucket_policy(Bucket="restore-target")["Policy"])
    matching = [s for s in policy["Statement"] if s["Sid"] == "AllowNzshmBatchRoleWrite"]
    assert len(matching) == 1
