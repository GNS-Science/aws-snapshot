"""Tests for the pitr-watcher Lambda (SSM-based pending restore discovery)."""

from unittest.mock import MagicMock, patch

from nzshm_backup.lambda_pitr_watcher import _process_source_entries, handler

REGION = "ap-southeast-2"
TABLE_NAME = "my-table-restore"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:123456789012:table/{TABLE_NAME}"

SOURCE_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:123456789012:table/my-table"
_ENTRY = {
    "restore_arn": TABLE_ARN,
    "source": "test-source",
    "source_table_arn": SOURCE_TABLE_ARN,
    "restore_point": "2026-03-15T09:00:00+00:00",
    "submitted_at": "2026-03-18T00:00:00+00:00",
}


def _make_dynamodb_client(table_status: str = "ACTIVE"):
    client = MagicMock()
    client.describe_table.return_value = {"Table": {"TableStatus": table_status}}
    return client


# ---------------------------------------------------------------------------
# _process_source_entries unit tests
# ---------------------------------------------------------------------------


def test_pitr_enabled_when_table_active():
    """ACTIVE table → PITR enabled, informational tags applied, entry removed from remaining."""
    dynamo = _make_dynamodb_client("ACTIVE")

    remaining, completed, still_pending = _process_source_entries(dynamo, [_ENTRY], "test-source")

    assert still_pending == 0
    assert remaining == []
    assert completed == [_ENTRY]
    dynamo.update_continuous_backups.assert_called_once_with(
        TableName=TABLE_NAME,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    tag_call = dynamo.tag_resource.call_args[1]
    tags = {t["Key"]: t["Value"] for t in tag_call["Tags"]}
    assert tags["RestoredBy"] == "nzshm-backup"
    assert tags["RestoredFrom"] == "my-table"
    assert tags["RestoredAt"] == "2026-03-15T09:00:00+00:00"
    assert tag_call["ResourceArn"] == TABLE_ARN


def test_still_pending_when_table_creating():
    """CREATING table → entry kept in remaining, still_pending=1."""
    dynamo = _make_dynamodb_client("CREATING")

    remaining, completed, still_pending = _process_source_entries(dynamo, [_ENTRY], "test-source")

    assert still_pending == 1
    assert remaining == [_ENTRY]
    assert completed == []
    dynamo.update_continuous_backups.assert_not_called()


def test_no_entries():
    """Empty entry list → nothing to do."""
    dynamo = _make_dynamodb_client()

    remaining, completed, still_pending = _process_source_entries(dynamo, [], "test-source")

    assert still_pending == 0
    assert remaining == []
    assert completed == []
    dynamo.describe_table.assert_not_called()


def test_describe_table_error_keeps_entry_pending():
    """describe_table raising an exception → entry kept, still_pending incremented."""
    dynamo = MagicMock()
    dynamo.describe_table.side_effect = Exception("Table not found")

    remaining, completed, still_pending = _process_source_entries(dynamo, [_ENTRY], "test-source")

    assert still_pending == 1
    assert remaining == [_ENTRY]
    assert completed == []
    dynamo.update_continuous_backups.assert_not_called()


def test_mixed_status_entries():
    """One ACTIVE, one CREATING → ACTIVE removed, CREATING kept."""
    arn_active = f"arn:aws:dynamodb:{REGION}:123456789012:table/active-table"
    arn_creating = f"arn:aws:dynamodb:{REGION}:123456789012:table/creating-table"
    entries = [
        {"restore_arn": arn_active, "source": "src", "submitted_at": "2026-03-18T00:00:00+00:00"},
        {"restore_arn": arn_creating, "source": "src", "submitted_at": "2026-03-18T00:00:00+00:00"},
    ]

    dynamo = MagicMock()
    dynamo.describe_table.side_effect = [
        {"Table": {"TableStatus": "ACTIVE"}},
        {"Table": {"TableStatus": "CREATING"}},
    ]

    remaining, completed, still_pending = _process_source_entries(dynamo, entries, "src")

    assert still_pending == 1
    assert len(remaining) == 1
    assert remaining[0]["restore_arn"] == arn_creating
    assert len(completed) == 1
    assert completed[0]["restore_arn"] == arn_active
    assert dynamo.update_continuous_backups.call_count == 1


# ---------------------------------------------------------------------------
# handler integration tests (mocked config + AWS clients)
# ---------------------------------------------------------------------------


def _make_source_config(has_dynamo=True):
    cfg = MagicMock()
    cfg.dynamodb_tables = (
        ["arn:aws:dynamodb:ap-southeast-2:123456789012:table/t1"] if has_dynamo else []
    )
    cfg.source_account_id = None
    cfg.source_account_role_arn = None
    cfg.source_account_restore_role_arn = None
    return cfg


def _make_config(sources: dict):
    cfg = MagicMock()
    cfg.sources = sources
    return cfg


@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_disables_rule_when_ssm_empty(mock_session_cls, mock_account_id, mock_config):
    """No pending entries in SSM → EventBridge rule disabled immediately."""
    mock_config.return_value = _make_config({"src": _make_source_config()})

    session = MagicMock()
    mock_session_cls.return_value = session

    ssm = MagicMock()
    from botocore.exceptions import ClientError

    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter"
    )
    events = MagicMock()
    clients = {"ssm": ssm, "events": events}
    session.client.side_effect = clients.__getitem__

    result = handler({}, None)

    assert result["statusCode"] == 200
    assert result["tables_found"] == 0
    assert result["still_pending"] == 0
    events.disable_rule.assert_called_once_with(Name="nzshm-backup-pitr-watcher")


@patch("nzshm_backup.lambda_pitr_watcher.append_event")
@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_enables_pitr_and_removes_entry(
    mock_session_cls, mock_account_id, mock_config, mock_append
):
    """ACTIVE table in SSM → PITR enabled, entry removed, rule disabled."""
    import json

    mock_config.return_value = _make_config({"test-source": _make_source_config()})

    session = MagicMock()
    mock_session_cls.return_value = session

    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps({"pending": [_ENTRY]})}}
    dynamo = _make_dynamodb_client("ACTIVE")
    events = MagicMock()
    clients = {"ssm": ssm, "dynamodb": dynamo, "events": events}
    session.client.side_effect = clients.__getitem__

    result = handler({}, None)

    assert result["tables_found"] == 1
    assert result["still_pending"] == 0
    dynamo.update_continuous_backups.assert_called_once()
    # SSM written back with empty list
    put_call = ssm.put_parameter.call_args
    written = json.loads(put_call[1]["Value"])
    assert written["pending"] == []
    events.disable_rule.assert_called_once_with(Name="nzshm-backup-pitr-watcher")


@patch("nzshm_backup.lambda_pitr_watcher._get_config")
@patch("nzshm_backup.lambda_pitr_watcher.get_account_id", return_value="123456789012")
@patch("boto3.Session")
def test_handler_keeps_rule_enabled_when_still_pending(
    mock_session_cls, mock_account_id, mock_config
):
    """CREATING table in SSM → rule stays enabled, entry preserved in SSM."""
    import json

    mock_config.return_value = _make_config({"test-source": _make_source_config()})

    session = MagicMock()
    mock_session_cls.return_value = session

    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps({"pending": [_ENTRY]})}}
    dynamo = _make_dynamodb_client("CREATING")
    events = MagicMock()
    clients = {"ssm": ssm, "dynamodb": dynamo, "events": events}
    session.client.side_effect = clients.__getitem__

    result = handler({}, None)

    assert result["still_pending"] == 1
    events.disable_rule.assert_not_called()
    put_call = ssm.put_parameter.call_args
    written = json.loads(put_call[1]["Value"])
    assert len(written["pending"]) == 1
