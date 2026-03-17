"""Tests for the pitr-watcher Lambda."""

from unittest.mock import MagicMock, call, patch

import pytest

from nzshm_backup.dynamodb_restore import PITR_PENDING_TAG
from nzshm_backup.lambda_pitr_watcher import _process_source, handler

REGION = "ap-southeast-2"
TABLE_NAME = "my-table-restored"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:123456789012:table/{TABLE_NAME}"


def _make_tagging_client(table_arns: list[str]):
    """Return a mock tagging client that reports the given table ARNs as PITRPending=true."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "ResourceTagMappingList": [
                {"ResourceARN": arn} for arn in table_arns
            ]
        }
    ]
    client.get_paginator.return_value = paginator
    return client


def _make_dynamodb_client(table_status: str = "ACTIVE"):
    client = MagicMock()
    client.describe_table.return_value = {"Table": {"TableStatus": table_status}}
    return client


# ---------------------------------------------------------------------------
# _process_source unit tests
# ---------------------------------------------------------------------------

def test_pitr_enabled_when_table_active():
    """ACTIVE table with PITRPending → PITR enabled, tag removed."""
    tagging = _make_tagging_client([TABLE_ARN])
    dynamo = _make_dynamodb_client("ACTIVE")

    found, still_pending = _process_source(tagging, dynamo, "test-source")

    assert found == 1
    assert still_pending == 0
    dynamo.update_continuous_backups.assert_called_once_with(
        TableName=TABLE_NAME,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    dynamo.untag_resource.assert_called_once_with(
        ResourceArn=TABLE_ARN,
        TagKeys=[PITR_PENDING_TAG],
    )


def test_still_pending_when_table_creating():
    """CREATING table → still_pending=1, nothing written."""
    tagging = _make_tagging_client([TABLE_ARN])
    dynamo = _make_dynamodb_client("CREATING")

    found, still_pending = _process_source(tagging, dynamo, "test-source")

    assert found == 1
    assert still_pending == 1
    dynamo.update_continuous_backups.assert_not_called()
    dynamo.untag_resource.assert_not_called()


def test_no_pending_tables():
    """No PITRPending tables → found=0, still_pending=0."""
    tagging = _make_tagging_client([])
    dynamo = _make_dynamodb_client()

    found, still_pending = _process_source(tagging, dynamo, "test-source")

    assert found == 0
    assert still_pending == 0
    dynamo.describe_table.assert_not_called()


def test_describe_table_error_increments_still_pending():
    """describe_table raising an exception → still_pending incremented, no crash."""
    tagging = _make_tagging_client([TABLE_ARN])
    dynamo = MagicMock()
    dynamo.describe_table.side_effect = Exception("Table not found")

    found, still_pending = _process_source(tagging, dynamo, "test-source")

    assert found == 1
    assert still_pending == 1
    dynamo.update_continuous_backups.assert_not_called()


def test_mixed_status_tables():
    """One ACTIVE, one CREATING → only ACTIVE gets PITR; still_pending=1."""
    arn_active = f"arn:aws:dynamodb:{REGION}:123456789012:table/active-table"
    arn_creating = f"arn:aws:dynamodb:{REGION}:123456789012:table/creating-table"

    tagging = _make_tagging_client([arn_active, arn_creating])
    dynamo = MagicMock()
    dynamo.describe_table.side_effect = [
        {"Table": {"TableStatus": "ACTIVE"}},
        {"Table": {"TableStatus": "CREATING"}},
    ]

    found, still_pending = _process_source(tagging, dynamo, "test-source")

    assert found == 2
    assert still_pending == 1
    assert dynamo.update_continuous_backups.call_count == 1
    assert dynamo.untag_resource.call_count == 1


# ---------------------------------------------------------------------------
# handler integration tests (mocked config + AWS clients)
# ---------------------------------------------------------------------------

def _make_source_config(has_dynamo=True):
    cfg = MagicMock()
    cfg.dynamodb_tables = ["arn:aws:dynamodb:ap-southeast-2:123456789012:table/t1"] if has_dynamo else []
    cfg.source_account_id = None
    cfg.source_account_role_arn = None
    return cfg


def _make_config(sources: dict):
    cfg = MagicMock()
    cfg.sources = sources
    return cfg


@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_disables_rule_when_no_pending(mock_session_cls, mock_account_id, mock_config):
    """No PITRPending tables across all sources → EventBridge rule disabled."""
    mock_config.return_value = _make_config({"src": _make_source_config()})

    session = MagicMock()
    mock_session_cls.return_value = session

    tagging = _make_tagging_client([])
    dynamo = _make_dynamodb_client()
    events = MagicMock()
    clients = {"resourcegroupstaggingapi": tagging, "dynamodb": dynamo, "events": events}
    session.client.side_effect = clients.__getitem__

    result = handler({}, None)

    assert result["statusCode"] == 200
    assert result["tables_found"] == 0
    assert result["still_pending"] == 0
    events.disable_rule.assert_called_once_with(Name="nzshm-backup-pitr-watcher")


@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_does_not_disable_rule_when_still_pending(mock_session_cls, mock_account_id, mock_config):
    """Tables still CREATING → rule stays enabled."""
    mock_config.return_value = _make_config({"src": _make_source_config()})

    session = MagicMock()
    mock_session_cls.return_value = session

    tagging = _make_tagging_client([TABLE_ARN])
    dynamo = _make_dynamodb_client("CREATING")
    events = MagicMock()
    clients = {"resourcegroupstaggingapi": tagging, "dynamodb": dynamo, "events": events}
    session.client.side_effect = clients.__getitem__

    result = handler({}, None)

    assert result["still_pending"] == 1
    events.disable_rule.assert_not_called()


@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_skips_sources_without_dynamodb(mock_session_cls, mock_account_id, mock_config):
    """Sources with no dynamodb_tables are skipped entirely."""
    mock_config.return_value = _make_config({"s3-only": _make_source_config(has_dynamo=False)})

    session = MagicMock()
    mock_session_cls.return_value = session
    events = MagicMock()
    session.client.return_value = events

    result = handler({}, None)

    assert result["tables_found"] == 0
    events.disable_rule.assert_called_once()
