"""Tests for DynamoDB restore operations."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from nzshm_backup.dynamodb_restore import (
    DynamoDBRestoreResult,
    DynamoDBRestoreStatus,
    describe_restore_status,
    make_restore_table_name,
    restore_dynamodb_table,
)

REGION = "ap-southeast-2"
ACCOUNT_ID = "123456789012"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/ToshiAPI-FileTable"
RESTORE_POINT = datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# make_restore_table_name
# ---------------------------------------------------------------------------


def test_make_restore_table_name_basic():
    result = make_restore_table_name(TABLE_ARN)
    assert result == "ToshiAPI-FileTable-restored"


def test_make_restore_table_name_truncates_long_name():
    long_name = "A" * 250
    long_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{long_name}"
    result = make_restore_table_name(long_arn)
    assert len(result) == 255
    assert result.endswith("-restored")


def test_make_restore_table_name_exact_max_base():
    """246-char base name + 9-char suffix = exactly 255."""
    name = "B" * 246
    arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{name}"
    result = make_restore_table_name(arn)
    assert len(result) == 255


# ---------------------------------------------------------------------------
# restore_dynamodb_table
# ---------------------------------------------------------------------------


def test_restore_dry_run_returns_skipped():
    client = MagicMock()
    result = restore_dynamodb_table(client, TABLE_ARN, "MyTable-restored", RESTORE_POINT, dry_run=True)

    assert result.status == "SKIPPED"
    assert result.dry_run is True
    assert result.success is True
    client.restore_table_to_point_in_time.assert_not_called()


def test_restore_initiated_on_success():
    client = MagicMock()
    client.restore_table_to_point_in_time.return_value = {
        "TableDescription": {
            "TableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/MyTable-restored",
            "TableStatus": "CREATING",
        }
    }

    result = restore_dynamodb_table(client, TABLE_ARN, "MyTable-restored", RESTORE_POINT)

    assert result.status == "INITIATED"
    assert result.success is True
    assert result.restore_arn is not None
    client.restore_table_to_point_in_time.assert_called_once_with(
        SourceTableArn=TABLE_ARN,
        TargetTableName="MyTable-restored",
        RestoreDateTime=RESTORE_POINT,
        BillingModeOverride="PAY_PER_REQUEST",
    )


def test_restore_failed_on_client_error():
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.restore_table_to_point_in_time.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
        "RestoreTableToPointInTime",
    )

    result = restore_dynamodb_table(client, TABLE_ARN, "MyTable-restored", RESTORE_POINT)

    assert result.status == "FAILED"
    assert result.success is False
    assert len(result.errors) == 1


def test_restore_result_fields():
    client = MagicMock()
    client.restore_table_to_point_in_time.return_value = {
        "TableDescription": {"TableArn": "arn:...", "TableStatus": "CREATING"}
    }
    result = restore_dynamodb_table(client, TABLE_ARN, "T-restored", RESTORE_POINT)

    assert result.source_table_arn == TABLE_ARN
    assert result.target_table_name == "T-restored"
    assert result.restore_point == RESTORE_POINT
    assert isinstance(result, DynamoDBRestoreResult)


# ---------------------------------------------------------------------------
# describe_restore_status
# ---------------------------------------------------------------------------


def test_describe_restore_status_restoring():
    client = MagicMock()
    client.describe_table.return_value = {
        "Table": {
            "TableStatus": "CREATING",
            "RestoreSummary": {
                "RestoreInProgress": True,
                "RestoreDateTime": RESTORE_POINT,
                "SourceTableArn": TABLE_ARN,
            },
        }
    }

    status = describe_restore_status(client, "MyTable-restored")

    assert isinstance(status, DynamoDBRestoreStatus)
    assert status.table_status == "CREATING"
    assert status.restore_in_progress is True
    assert status.restore_date_time == RESTORE_POINT
    assert status.source_table_arn == TABLE_ARN
    assert status.restore_status == "RESTORING"


def test_describe_restore_status_completed():
    client = MagicMock()
    client.describe_table.return_value = {
        "Table": {
            "TableStatus": "ACTIVE",
            "RestoreSummary": {
                "RestoreInProgress": False,
                "RestoreDateTime": RESTORE_POINT,
                "SourceTableArn": TABLE_ARN,
            },
        }
    }

    status = describe_restore_status(client, "MyTable-restored")

    assert status.table_status == "ACTIVE"
    assert status.restore_in_progress is False
    assert status.restore_status == "SUCCEEDED"


def test_describe_restore_status_no_restore_summary():
    """Table exists but has no RestoreSummary (not a restored table)."""
    client = MagicMock()
    client.describe_table.return_value = {
        "Table": {"TableStatus": "ACTIVE"}
    }

    status = describe_restore_status(client, "some-table")

    assert status.restore_in_progress is False
    assert status.restore_date_time is None
    assert status.restore_status is None
