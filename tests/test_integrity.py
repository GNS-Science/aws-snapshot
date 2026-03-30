"""Tests for backup integrity checking using moto mocks."""

import boto3
from moto import mock_aws

from nzshm_backup.integrity import IntegrityResult, ObjectDiff, check_bucket_integrity

REGION = "ap-southeast-2"


def _make_bucket(s3_client, name: str) -> str:
    s3_client.create_bucket(
        Bucket=name,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    return name


@mock_aws
def test_clean_buckets_returns_no_diffs():
    """Identical source and backup → clean result."""
    s3 = boto3.client("s3", region_name=REGION)
    _make_bucket(s3, "source")
    _make_bucket(s3, "backup")
    for key, body in [("a.txt", b"aaa"), ("b.txt", b"bbb")]:
        s3.put_object(Bucket="source", Key=key, Body=body)
        s3.copy_object(CopySource={"Bucket": "source", "Key": key}, Bucket="backup", Key=key)

    result = check_bucket_integrity(s3, "source", "backup")

    assert result.clean
    assert result.diffs == []
    assert result.source_object_count == 2
    assert result.backup_object_count == 2


@mock_aws
def test_missing_object_flagged():
    """Object in source but absent from backup → missing_in_backup diff."""
    s3 = boto3.client("s3", region_name=REGION)
    _make_bucket(s3, "source")
    _make_bucket(s3, "backup")
    s3.put_object(Bucket="source", Key="present.txt", Body=b"here")
    s3.put_object(Bucket="source", Key="missing.txt", Body=b"gone")
    s3.copy_object(
        CopySource={"Bucket": "source", "Key": "present.txt"}, Bucket="backup", Key="present.txt"
    )

    result = check_bucket_integrity(s3, "source", "backup")

    assert not result.clean
    assert result.missing_count == 1
    assert result.diffs[0].key == "missing.txt"
    assert result.diffs[0].issue == "missing_in_backup"


@mock_aws
def test_etag_mismatch_flagged():
    """Same key but different body (different ETag) → etag_mismatch diff."""
    s3 = boto3.client("s3", region_name=REGION)
    _make_bucket(s3, "source")
    _make_bucket(s3, "backup")
    s3.put_object(Bucket="source", Key="file.txt", Body=b"original")
    s3.put_object(Bucket="backup", Key="file.txt", Body=b"mutated")

    result = check_bucket_integrity(s3, "source", "backup")

    assert not result.clean
    assert result.mismatch_count == 1
    diff = result.diffs[0]
    assert diff.key == "file.txt"
    assert diff.issue == "etag_mismatch"
    assert diff.source_etag != diff.backup_etag


@mock_aws
def test_operational_prefixes_excluded():
    """Objects under _state/, _manifests/, _batch-reports/ are not compared."""
    s3 = boto3.client("s3", region_name=REGION)
    _make_bucket(s3, "source")
    _make_bucket(s3, "backup")
    # backup-only operational objects should not be flagged as 'extra'
    s3.put_object(Bucket="backup", Key="_state/last-run.json", Body=b"{}")
    s3.put_object(Bucket="backup", Key="_manifests/m1.csv", Body=b"csv")
    s3.put_object(Bucket="backup", Key="_batch-reports/r1.csv", Body=b"csv")

    result = check_bucket_integrity(s3, "source", "backup")

    assert result.clean
    assert result.backup_object_count == 0  # operational objects excluded from count


@mock_aws
def test_extra_backup_objects_not_flagged():
    """Objects in backup but absent from source (deleted at source) are not flagged."""
    s3 = boto3.client("s3", region_name=REGION)
    _make_bucket(s3, "source")
    _make_bucket(s3, "backup")
    s3.put_object(Bucket="source", Key="present.txt", Body=b"here")
    s3.put_object(Bucket="backup", Key="present.txt", Body=b"here")
    s3.copy_object(
        CopySource={"Bucket": "backup", "Key": "present.txt"},
        Bucket="backup",
        Key="deleted-at-source.txt",
    )

    result = check_bucket_integrity(s3, "source", "backup")

    assert result.clean
    assert result.diffs == []


@mock_aws
def test_cross_account_client_used_for_source():
    """When source_s3_client is provided it is used to list the source bucket."""
    source_s3 = boto3.client("s3", region_name=REGION)
    backup_s3 = boto3.client("s3", region_name=REGION)

    source_s3.create_bucket(
        Bucket="source", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    backup_s3.create_bucket(
        Bucket="backup", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    source_s3.put_object(Bucket="source", Key="x.txt", Body=b"x")
    backup_s3.copy_object(
        CopySource={"Bucket": "source", "Key": "x.txt"}, Bucket="backup", Key="x.txt"
    )

    # Pass source_s3 explicitly to simulate cross-account
    result = check_bucket_integrity(backup_s3, "source", "backup", source_s3_client=source_s3)

    assert result.clean
    assert result.source_object_count == 1


@mock_aws
def test_source_bucket_error_captured():
    """ClientError listing source → recorded in errors, result not clean."""
    from unittest.mock import MagicMock

    from botocore.exceptions import ClientError

    real_s3 = boto3.client("s3", region_name=REGION)
    real_s3.create_bucket(Bucket="backup", CreateBucketConfiguration={"LocationConstraint": REGION})

    bad_s3 = MagicMock()
    bad_s3.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "gone"}}, "ListObjectsV2"
    )

    result = check_bucket_integrity(real_s3, "source", "backup", source_s3_client=bad_s3)

    assert not result.clean
    assert len(result.errors) == 1


@mock_aws
def test_integrity_result_properties():
    """missing_count and mismatch_count properties sum correctly."""
    result = IntegrityResult(source_bucket="s", backup_bucket="b")
    result.diffs = [
        ObjectDiff(key="a", issue="missing_in_backup"),
        ObjectDiff(key="b", issue="missing_in_backup"),
        ObjectDiff(key="c", issue="etag_mismatch"),
    ]
    assert result.missing_count == 2
    assert result.mismatch_count == 1
    assert not result.clean
