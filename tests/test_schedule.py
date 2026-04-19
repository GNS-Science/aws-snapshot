"""Tests for schedule management commands (EventBridge)."""

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from nzshm_backup.commands.schedule import app
from nzshm_backup.state import AppState

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


def test_show_displays_target_mode_and_detail(events_client):
    """show text output should include target mode and detail columns."""
    rule_name = _make_rule(events_client, "toshi", "weekly")
    events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "backup-lambda",
                "Arn": (
                    "arn:aws:lambda:ap-southeast-2:123456789012:"
                    "function:nzshm-backup-service-prod-backup"
                ),
            }
        ],
    )

    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "Target" in result.output
    assert "lambda" in result.output
    assert "nzshm-backup-service-prod-backup" in result.output


def test_show_json_includes_target_metadata(events_client):
    """show --output json should include target_type and targets list."""
    rule_name = _make_rule(events_client, "ths", "weekly")
    events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "backup-codebuild",
                "Arn": (
                    "arn:aws:codebuild:ap-southeast-2:123456789012:project/nzshm-backup-ths-backup"
                ),
                "RoleArn": "arn:aws:iam::123456789012:role/nzshm-backup-events-codebuild",
            }
        ],
    )

    with patch("nzshm_backup.commands.schedule.get_state", return_value=AppState(output="json")):
        result = runner.invoke(app, ["show"])
    assert result.exit_code == 0

    data = json.loads(result.output)
    row = [r for r in data if r["Name"] == rule_name][0]
    assert row["target_type"] == "codebuild"
    assert len(row["targets"]) == 1
    assert row["targets"][0]["Id"] == "backup-codebuild"


def test_add_creates_weekly_rule(events_client):
    """add --frequency weekly should create a rule with a weekly cron expression."""
    result = runner.invoke(
        app, ["add", "--source", "toshi", "--frequency", "weekly", "--time", "14:00"]
    )
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-weekly")["Rules"]
    assert len(rules) == 1
    assert rules[0]["Name"] == "nzshm-backup-toshi-weekly"
    assert "SUN" in rules[0]["ScheduleExpression"]
    assert "14" in rules[0]["ScheduleExpression"]


def test_add_creates_daily_rule(events_client):
    """add --frequency daily should create a rule with a daily cron expression."""
    result = runner.invoke(
        app, ["add", "--source", "ths", "--frequency", "daily", "--time", "03:30"]
    )
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-ths-daily")["Rules"]
    assert len(rules) == 1
    assert "SUN" not in rules[0]["ScheduleExpression"]
    assert "3" in rules[0]["ScheduleExpression"]
    assert "30" in rules[0]["ScheduleExpression"]


def test_add_creates_hourly_rule(events_client):
    """add --frequency hourly should create a rule with '*' for the hour field."""
    result = runner.invoke(
        app, ["add", "--source", "toshi", "--frequency", "hourly", "--time", "00:05"]
    )
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-hourly")["Rules"]
    assert len(rules) == 1
    expr = rules[0]["ScheduleExpression"]
    assert expr.startswith("cron(5 *")
    assert "SUN" not in expr


def test_add_creates_minutely_rule(events_client):
    """add --frequency minutely should create a rate(1 minute) rule."""
    result = runner.invoke(app, ["add", "--source", "toshi", "--frequency", "minutely"])
    assert result.exit_code == 0

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-minutely")["Rules"]
    assert len(rules) == 1
    assert rules[0]["ScheduleExpression"] == "rate(1 minute)"


def test_add_without_lambda_arn(events_client):
    """add should create the rule but warn if lambda_arn is not configured."""
    result = runner.invoke(
        app, ["add", "--source", "toshi", "--frequency", "daily", "--time", "02:00"]
    )
    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-daily")["Rules"]
    assert len(rules) == 1
    assert "lambda_arn" in result.output or "Warning" in result.output


def test_add_codebuild_target_registers_rule_target(events_client):
    """add --target codebuild should register CodeBuild as the EventBridge target."""
    project_arn = "arn:aws:codebuild:ap-southeast-2:123456789012:project/nzshm-backup-ths"
    role_arn = "arn:aws:iam::123456789012:role/nzshm-backup-events-codebuild"

    result = runner.invoke(
        app,
        [
            "add",
            "--source",
            "ths",
            "--frequency",
            "weekly",
            "--time",
            "14:00",
            "--target",
            "codebuild",
            "--codebuild-project-arn",
            project_arn,
            "--target-role-arn",
            role_arn,
        ],
    )
    assert result.exit_code == 0

    targets = events_client.list_targets_by_rule(Rule="nzshm-backup-ths-weekly")["Targets"]
    assert len(targets) == 1
    assert targets[0]["Id"] == "backup-codebuild"
    assert targets[0]["Arn"] == project_arn
    assert targets[0]["RoleArn"] == role_arn


def test_add_codebuild_requires_project_and_role(events_client):
    """CodeBuild target should fail fast when required target ARNs are missing."""
    result = runner.invoke(
        app,
        [
            "add",
            "--source",
            "ths",
            "--frequency",
            "daily",
            "--time",
            "03:30",
            "--target",
            "codebuild",
        ],
    )
    assert result.exit_code == 1
    assert "requires --codebuild-project-arn and --target-role-arn" in result.output


def test_add_replaces_existing_targets_when_switching_modes(events_client):
    """Re-adding a schedule with a different target should replace prior targets."""
    rule_name = _make_rule(events_client, "ths", "weekly")
    events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "backup-lambda",
                "Arn": "arn:aws:lambda:ap-southeast-2:123456789012:function:backup",
            }
        ],
    )

    result = runner.invoke(
        app,
        [
            "add",
            "--source",
            "ths",
            "--frequency",
            "weekly",
            "--time",
            "14:00",
            "--target",
            "codebuild",
            "--codebuild-project-arn",
            "arn:aws:codebuild:ap-southeast-2:123456789012:project/nzshm-backup-ths",
            "--target-role-arn",
            "arn:aws:iam::123456789012:role/nzshm-backup-events-codebuild",
        ],
    )
    assert result.exit_code == 0

    targets = events_client.list_targets_by_rule(Rule=rule_name)["Targets"]
    assert len(targets) == 1
    assert targets[0]["Id"] == "backup-codebuild"


def test_remove_deletes_rule(events_client):
    """remove should delete the EventBridge rule."""
    _make_rule(events_client, "toshi", "daily")

    result = runner.invoke(app, ["remove", "--source", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0
    assert "deleted" in result.output.lower()

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-daily")["Rules"]
    assert len(rules) == 0


def test_remove_nonexistent_rule():
    """remove on a nonexistent rule should not raise."""
    result = runner.invoke(app, ["remove", "--source", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0


def test_enable_rule(events_client):
    """enable should set rule State to ENABLED."""
    _make_rule(events_client, "toshi", "daily", state="DISABLED")

    result = runner.invoke(app, ["enable", "--source", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0
    assert "Enabled" in result.output

    rules = events_client.list_rules(NamePrefix="nzshm-backup-toshi-daily")["Rules"]
    assert rules[0]["State"] == "ENABLED"


def test_disable_rule(events_client):
    """disable should set rule State to DISABLED."""
    _make_rule(events_client, "ths", "weekly", state="ENABLED")

    result = runner.invoke(app, ["disable", "--source", "ths", "--frequency", "weekly"])
    assert result.exit_code == 0
    assert "Disabled" in result.output

    rules = events_client.list_rules(NamePrefix="nzshm-backup-ths-weekly")["Rules"]
    assert rules[0]["State"] == "DISABLED"


def test_enable_nonexistent_rule():
    """enable on a nonexistent rule should skip gracefully, not raise."""
    result = runner.invoke(app, ["enable", "--source", "toshi", "--frequency", "daily"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower() or "skipping" in result.output.lower()
