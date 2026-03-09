"""Tests for schedule management commands (EventBridge)."""

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from nzshm_backup.commands.schedule import app

REGION = "ap-southeast-2"
runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_aws_session():
    """Activate moto mock for all tests in this module."""
    with mock_aws():
        yield


@pytest.fixture
def events_client():
    return boto3.client("events", region_name=REGION)


def _make_rule(events_client, source: str, frequency: str, state: str = "ENABLED") -> str:
    rule_name = f"nzshm-backup-{source}-{frequency}"
    events_client.put_rule(
        Name=rule_name,
        ScheduleExpression="cron(0 2 * * ? *)",
        State=state,
    )
    return rule_name


def test_show_no_rules():
    """show with no matching rules should output a clean 'not found' message."""
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "No backup schedules found" in result.output


def test_show_lists_rules(events_client):
    """show should list existing nzshm-backup- rules."""
    _make_rule(events_client, "toshi", "weekly")
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "nzshm-backup-toshi-weekly" in result.output


def test_set_creates_weekly_rule(events_client):
    """set --frequency weekly should create a rule with a weekly cron expression."""
    result = runner.invoke(
        app, ["set", "--source", "toshi", "--frequency", "weekly", "--time", "14:00"]
    )
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-weekly")["Rules"]
    assert len(rules) == 1
    assert rules[0]["Name"] == "nzshm-backup-toshi-weekly"
    assert "SUN" in rules[0]["ScheduleExpression"]
    assert "14" in rules[0]["ScheduleExpression"]


def test_set_creates_daily_rule(events_client):
    """set --frequency daily should create a rule with a daily cron expression."""
    result = runner.invoke(
        app, ["set", "--source", "ths", "--frequency", "daily", "--time", "03:30"]
    )
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-ths-daily")["Rules"]
    assert len(rules) == 1
    assert "SUN" not in rules[0]["ScheduleExpression"]
    assert "3" in rules[0]["ScheduleExpression"]
    assert "30" in rules[0]["ScheduleExpression"]


def test_enable_rule(events_client):
    """enable should set rule State to ENABLED."""
    _make_rule(events_client, "toshi", "daily", state="DISABLED")

    result = runner.invoke(app, ["enable", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0
    assert "Enabled" in result.output

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-daily")["Rules"]
    assert rules[0]["State"] == "ENABLED"


def test_disable_rule(events_client):
    """disable should set rule State to DISABLED."""
    _make_rule(events_client, "ths", "weekly", state="ENABLED")

    result = runner.invoke(app, ["disable", "ths", "--frequency", "weekly"])
    assert result.exit_code == 0
    assert "Disabled" in result.output

    rules = events_client.list_rules(NamePrefix="nzshm-backup-ths-weekly")["Rules"]
    assert rules[0]["State"] == "DISABLED"


def test_enable_nonexistent_rule():
    """enable on a nonexistent rule should skip gracefully, not raise."""
    result = runner.invoke(app, ["enable", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower() or "skipping" in result.output.lower()


def test_set_without_lambda_arn(events_client):
    """set should create the rule but warn if lambda_arn is not configured."""
    result = runner.invoke(
        app, ["set", "--source", "toshi", "--frequency", "daily", "--time", "02:00"]
    )
    # Rule should still be created
    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-daily")["Rules"]
    assert len(rules) == 1
    # Warning about missing lambda_arn
    assert "lambda_arn" in result.output or "Warning" in result.output
