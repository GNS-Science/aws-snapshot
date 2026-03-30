"""Tests for backup_engine.run_backup_source using moto mocks."""

import boto3
import pytest
from moto import mock_aws

from nzshm_backup.backup_engine import SourceBackupResult, run_backup_source
from nzshm_backup.config.models import ConfigModel, GeneralConfig, S3BucketConfig, SourceConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGION = "ap-southeast-2"
ACCOUNT_ID = "123456789012"
TABLE_NAME = "EngineTestTable"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}"
SOURCE_BUCKET = "engine-source-bucket"
SOURCE_BUCKET_ARN = f"arn:aws:s3:::{SOURCE_BUCKET}"


def _make_config(
    *,
    s3_buckets: list[S3BucketConfig] | None = None,
    dynamodb_tables: list[str] | None = None,
    source_account_role_arn: str | None = None,
    source_account_id: str | None = None,
) -> ConfigModel:
    """Build a minimal ConfigModel for testing."""
    return ConfigModel(
        general=GeneralConfig(region=REGION),
        sources={
            "testsrc": SourceConfig(
                display_name="Test Source",
                s3_buckets=s3_buckets or [],
                dynamodb_tables=dynamodb_tables or [],
                source_account_role_arn=source_account_role_arn,
                source_account_id=source_account_id,
            )
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_source_backup_result_success_property():
    """SourceBackupResult.success is True when errors is empty."""
    r = SourceBackupResult(source_alias="x")
    assert r.success is True

    r.errors.append("something failed")
    assert r.success is False


def test_backup_engine_unknown_source_raises():
    """run_backup_source raises KeyError for an unknown source alias."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        config = _make_config()

        with pytest.raises(KeyError):
            run_backup_source(session, config, "nonexistent_source")


def test_backup_engine_no_resources_returns_empty_result():
    """Source with no buckets or tables returns a successful empty result."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        config = _make_config()

        result = run_backup_source(session, config, "testsrc", dry_run=True)

    assert isinstance(result, SourceBackupResult)
    assert result.source_alias == "testsrc"
    assert result.s3_results == []
    assert result.dynamodb_results == []
    assert result.success is True


def test_backup_engine_dry_run_s3_no_writes():
    """Dry run with an S3 bucket counts objects but does not create backup bucket."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        s3 = session.client("s3", region_name=REGION)

        # Create the source bucket and populate it
        s3.create_bucket(
            Bucket=SOURCE_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        s3.put_object(Bucket=SOURCE_BUCKET, Key="a.txt", Body=b"hello")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="b.txt", Body=b"world")

        config = _make_config(s3_buckets=[S3BucketConfig(arn=SOURCE_BUCKET_ARN, label="engine")])

        result = run_backup_source(session, config, "testsrc", dry_run=True)

        assert result.success is True
        assert len(result.s3_results) == 1
        r = result.s3_results[0]
        assert r["status"] == "success"
        assert r["dry_run"] is True
        assert r["objects_copied"] == 2

        # Backup bucket must NOT have been created
        all_buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        backup_bucket = config.sources["testsrc"].get_backup_bucket_name(
            "engine", REGION, ACCOUNT_ID, "testsrc"
        )
        assert backup_bucket not in all_buckets


def test_backup_engine_dry_run_dynamodb_no_export():
    """Dry run with a DynamoDB table returns SKIPPED status, no real export."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)

        dynamodb = session.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.update_continuous_backups(
            TableName=TABLE_NAME,
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
        )

        config = _make_config(dynamodb_tables=[TABLE_ARN])

        result = run_backup_source(session, config, "testsrc", dry_run=True)

        assert result.success is True
        assert len(result.dynamodb_results) == 1
        dr = result.dynamodb_results[0]
        assert dr["status"] == "success"
        assert dr["dry_run"] is True
        assert dr["table_name"] == TABLE_NAME
        assert dr["export_arn"] is None  # SKIPPED in dry run

        # Export bucket must NOT have been created during dry run
        s3 = session.client("s3", region_name=REGION)
        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        export_bucket = config.sources["testsrc"].get_dynamodb_backup_bucket_name(
            "testsrc", REGION, ACCOUNT_ID  # bb-testsrc-dynamo-{region}-{account}
        )
        assert export_bucket not in buckets


def test_backup_engine_s3_error_captured_in_result():
    """An S3 backup error is recorded in s3_results and result.errors, not raised."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        # Source bucket does NOT exist — backup_source will raise ValueError
        config = _make_config(
            s3_buckets=[S3BucketConfig(arn="arn:aws:s3:::nonexistent-bucket-xyz", label="x")]
        )

        result = run_backup_source(session, config, "testsrc", dry_run=False)

    assert result.success is False
    assert len(result.s3_results) == 1
    assert result.s3_results[0]["status"] == "error"
    assert "nonexistent-bucket-xyz" in result.s3_results[0]["error"] or len(result.errors) > 0
    assert len(result.errors) == 1


def test_backup_engine_result_includes_bucket_name_key():
    """s3_results entries always carry a 'bucket_name' key for lambda handler keying."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        s3 = session.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=SOURCE_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

        config = _make_config(s3_buckets=[S3BucketConfig(arn=SOURCE_BUCKET_ARN, label="engine")])
        result = run_backup_source(session, config, "testsrc", dry_run=True)

    assert "bucket_name" in result.s3_results[0]
    assert result.s3_results[0]["bucket_name"] == SOURCE_BUCKET


def test_backup_engine_result_includes_table_name_key():
    """dynamodb_results entries always carry a 'table_name' key for lambda handler keying."""
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        config = _make_config(dynamodb_tables=[TABLE_ARN])
        result = run_backup_source(session, config, "testsrc", dry_run=True)

    assert "table_name" in result.dynamodb_results[0]
    assert result.dynamodb_results[0]["table_name"] == TABLE_NAME
