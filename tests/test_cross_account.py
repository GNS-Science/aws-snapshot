"""Tests for cross-account backup support."""

import boto3
import pytest
from moto import mock_aws

from aws_snapshot.s3_backup import (
    backup_source,
    get_cross_account_session,
    sync_bucket,
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


def _make_bucket(s3_client, name):
    s3_client.create_bucket(
        Bucket=name,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )
    return name


def _put(s3_client, bucket, key, body=b"data"):
    s3_client.put_object(Bucket=bucket, Key=key, Body=body)


# ---------------------------------------------------------------------------
# get_cross_account_session
# ---------------------------------------------------------------------------


def test_get_cross_account_session_returns_session(aws_session):
    """get_cross_account_session returns a boto3 Session using assumed-role creds."""
    # moto's sts:AssumeRole always succeeds regardless of the role ARN
    role_arn = "arn:aws:iam::456789012345:role/nzshm-backup-reader"
    cross_session = get_cross_account_session(aws_session, role_arn)

    assert isinstance(cross_session, boto3.Session)
    creds = cross_session.get_credentials().get_frozen_credentials()
    assert creds.access_key
    assert creds.secret_key
    assert creds.token  # session token present from AssumeRole


def test_get_cross_account_session_preserves_region(aws_session):
    """Assumed-role session inherits the caller's region."""
    role_arn = "arn:aws:iam::456789012345:role/nzshm-backup-reader"
    cross_session = get_cross_account_session(aws_session, role_arn)
    assert cross_session.region_name == "ap-southeast-2"


# ---------------------------------------------------------------------------
# sync_bucket — cross-account (separate source_s3_client)
# ---------------------------------------------------------------------------


def test_sync_bucket_cross_account_copies_objects(s3_client):
    """sync_bucket uses source_s3_client to read and dest client to write."""
    _make_bucket(s3_client, "source-bucket")
    _make_bucket(s3_client, "backup-bucket")
    _put(s3_client, "source-bucket", "file.txt", b"hello")

    # In moto all clients share the same state, so using same client as both
    # source and dest still exercises the cross-account code path
    result = sync_bucket(
        s3_client,
        "source-bucket",
        "backup-bucket",
        source_s3_client=s3_client,
    )

    assert result.objects_copied == 1
    assert result.success is True

    obj = s3_client.get_object(Bucket="backup-bucket", Key="file.txt")
    assert obj["Body"].read() == b"hello"


def test_sync_bucket_cross_account_dry_run(s3_client):
    """Cross-account dry run counts objects without writing."""
    from botocore.exceptions import ClientError

    _make_bucket(s3_client, "source-xacct")
    _make_bucket(s3_client, "backup-xacct")
    _put(s3_client, "source-xacct", "a.txt")
    _put(s3_client, "source-xacct", "b.txt")

    result = sync_bucket(
        s3_client,
        "source-xacct",
        "backup-xacct",
        dry_run=True,
        source_s3_client=s3_client,
    )

    assert result.objects_copied == 2
    assert result.dry_run is True

    with pytest.raises(ClientError):
        s3_client.head_object(Bucket="backup-xacct", Key="a.txt")


def test_sync_bucket_cross_account_skips_unchanged(s3_client):
    """Cross-account sync skips objects already in backup with matching ETag."""
    _make_bucket(s3_client, "src-unchanged")
    _make_bucket(s3_client, "bak-unchanged")
    _put(s3_client, "src-unchanged", "same.txt", b"same")
    s3_client.copy_object(
        CopySource={"Bucket": "src-unchanged", "Key": "same.txt"},
        Bucket="bak-unchanged",
        Key="same.txt",
    )

    result = sync_bucket(
        s3_client,
        "src-unchanged",
        "bak-unchanged",
        source_s3_client=s3_client,
    )

    assert result.objects_copied == 0
    assert result.objects_skipped == 1


# ---------------------------------------------------------------------------
# backup_source — cross-account via source_session
# ---------------------------------------------------------------------------


def test_backup_source_cross_account(aws_session, s3_client):
    """backup_source with source_session uses cross-account clients."""
    _make_bucket(s3_client, "xacct-source")
    _put(s3_client, "xacct-source", "data.json", b"{}")

    backup_name = "xacct-source-backup-ap-southeast-2-123456789012"

    result = backup_source(
        session=aws_session,
        source_bucket="xacct-source",
        backup_bucket_name=backup_name,
        dry_run=False,
        source_session=aws_session,  # same session in moto; exercises the code path
    )

    assert result.objects_copied == 1
    assert result.success is True


def test_backup_source_no_source_session_unchanged(aws_session, s3_client):
    """backup_source without source_session behaves identically to before."""
    _make_bucket(s3_client, "same-acct-source")
    _put(s3_client, "same-acct-source", "file.txt")

    backup_name = "same-acct-source-backup-ap-southeast-2-123456789012"

    result = backup_source(
        session=aws_session,
        source_bucket="same-acct-source",
        backup_bucket_name=backup_name,
        dry_run=False,
        source_session=None,
    )

    assert result.objects_copied == 1


# ---------------------------------------------------------------------------
# Config: source_account_role_arn field
# ---------------------------------------------------------------------------


def test_config_source_account_role_arn_optional():
    """source_account_role_arn defaults to None (same-account backup)."""
    from aws_snapshot.config.models import SourceConfig

    sc = SourceConfig(display_name="test", s3_buckets=[], dynamodb_tables=[])
    assert sc.source_account_role_arn is None


def test_config_source_account_role_arn_set():
    """source_account_role_arn accepts a valid ARN."""
    from aws_snapshot.config.models import SourceConfig

    sc = SourceConfig(
        display_name="arkivalist",
        s3_buckets=[],
        dynamodb_tables=[],
        source_account_role_arn="arn:aws:iam::456789012345:role/nzshm-backup-reader",
    )
    assert "456789012345" in sc.source_account_role_arn
