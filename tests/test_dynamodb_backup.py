"""Tests for DynamoDB backup operations using moto mocks."""

import boto3
import pytest
from moto import mock_aws

from nzshm_backup.dynamodb_backup import (
    ensure_dynamodb_backup_bucket_ready,
    export_dynamodb_table,
)

REGION = "ap-southeast-2"
ACCOUNT_ID = "123456789012"
TABLE_NAME = "TestTable"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}"
EXPORT_BUCKET = "test-dynamo-export-bucket"


@pytest.fixture
def aws_session():
    """Create mocked AWS session."""
    with mock_aws():
        yield boto3.Session(region_name=REGION)


@pytest.fixture
def dynamodb_client(aws_session):
    """Create DynamoDB table with PITR enabled."""
    client = aws_session.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.update_continuous_backups(
        TableName=TABLE_NAME,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    return client


@pytest.fixture
def export_bucket(aws_session):
    """Create S3 export bucket."""
    s3 = aws_session.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket=EXPORT_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    return EXPORT_BUCKET


def test_export_dry_run(dynamodb_client):
    """Dry run should not call API, status should be SKIPPED, export_arn None."""
    result = export_dynamodb_table(dynamodb_client, TABLE_ARN, EXPORT_BUCKET, dry_run=True)

    assert result.status == "SKIPPED"
    assert result.export_arn is None
    assert result.dry_run is True
    assert result.success is True
    assert result.table_name == TABLE_NAME


def test_export_initiated(dynamodb_client, export_bucket, monkeypatch):
    """Successful export should return INITIATED status with an export ARN."""
    fake_arn = (
        f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}/export/01700000000000-abc123"
    )

    def mock_export(**kwargs):
        return {"ExportDescription": {"ExportArn": fake_arn}}

    monkeypatch.setattr(dynamodb_client, "export_table_to_point_in_time", mock_export)

    result = export_dynamodb_table(dynamodb_client, TABLE_ARN, EXPORT_BUCKET, dry_run=False)

    assert result.status == "INITIATED"
    assert result.export_arn == fake_arn
    assert result.success is True
    assert result.errors == []


def test_export_missing_table(dynamodb_client, export_bucket, monkeypatch):
    """Export of non-existent table should return FAILED status with errors populated."""
    from botocore.exceptions import ClientError

    def mock_export_fail(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "ExportTableToPointInTime",
        )

    monkeypatch.setattr(dynamodb_client, "export_table_to_point_in_time", mock_export_fail)

    result = export_dynamodb_table(
        dynamodb_client,
        "arn:aws:dynamodb:ap-southeast-2:123456789012:table/NonExistentTable",
        EXPORT_BUCKET,
        dry_run=False,
    )

    assert result.status == "FAILED"
    assert len(result.errors) == 1
    assert result.success is False


def test_ensure_bucket_creates_new(aws_session):
    """ensure_dynamodb_backup_bucket_ready should create a new bucket if missing."""
    s3 = aws_session.client("s3", region_name=REGION)
    bucket_name = "new-dynamo-export-bucket"

    # Confirm it doesn't exist
    existing = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket_name not in existing

    ensure_dynamodb_backup_bucket_ready(aws_session, bucket_name)

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket_name in buckets


def test_ensure_bucket_existing_is_idempotent(aws_session):
    """ensure_dynamodb_backup_bucket_ready should not error if bucket already exists."""
    s3 = aws_session.client("s3", region_name=REGION)
    bucket_name = "existing-dynamo-export-bucket"
    s3.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )

    # Should not raise
    ensure_dynamodb_backup_bucket_ready(aws_session, bucket_name)

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket_name in buckets
