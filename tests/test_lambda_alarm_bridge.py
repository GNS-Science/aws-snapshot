"""Tests for the SNS -> Slack alarm-bridge Lambda."""

from __future__ import annotations

import json
from unittest.mock import patch

from aws_snapshot import lambda_alarm_bridge as ab


def _sns_event(message: dict) -> dict:
    return {
        "Records": [
            {
                "Sns": {
                    "Subject": "ALARM: test",
                    "Message": json.dumps(message),
                }
            }
        ]
    }


def test_handler_delivers_alarm_to_slack():
    event = _sns_event(
        {
            "AlarmName": "nzshm-backup-lambda-log-errors-prod",
            "NewStateValue": "ALARM",
            "NewStateReason": (
                "Threshold crossed: 1 datapoint [3.0 (…)] was greater than the threshold (1.0)."
            ),
            "Region": "ap-southeast-2",
            "StateChangeTime": "2026-06-23T01:23:45Z",
        }
    )

    with (
        patch.object(ab, "resolve_webhook_url", return_value="https://hooks.slack.test/X") as r,
        patch.object(ab, "send_slack") as s,
    ):
        result = ab.handler(event, None)

    assert result == {"statusCode": 200, "body": "delivered=1 skipped=0"}
    r.assert_called_once()
    s.assert_called_once()
    webhook_url, blocks = s.call_args[0][0], s.call_args[0][1]
    assert webhook_url == "https://hooks.slack.test/X"
    assert blocks[0]["type"] == "header"
    assert "nzshm-backup-lambda-log-errors-prod" in blocks[0]["text"]["text"]
    assert "ALARM" in blocks[0]["text"]["text"]
    assert "rotating_light" in blocks[0]["text"]["text"]
    # Reason section preserves the alarm reason text
    assert "Threshold crossed" in blocks[1]["text"]["text"]
    # Context block includes region and timestamp
    assert "ap-southeast-2" in blocks[2]["elements"][0]["text"]


def test_handler_delivers_alarm_to_discord_when_secret_env_set(monkeypatch):
    event = _sns_event(
        {
            "AlarmName": "nzshm-backup-lambda-log-errors-prod",
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "Region": "ap-southeast-2",
            "StateChangeTime": "2026-06-27T02:30:00Z",
        }
    )

    monkeypatch.setenv("DISCORD_WEBHOOK_SECRET_ID", "backup-discord-webhook")

    with (
        patch.object(
            ab, "resolve_discord_webhook_url", return_value="https://discord.com/api/webhooks/1/2"
        ) as r,
        patch.object(ab, "send_discord") as s,
        patch.object(ab, "send_slack") as slack_mock,
    ):
        result = ab.handler(event, None)

    assert result == {"statusCode": 200, "body": "delivered=1 skipped=0"}
    r.assert_called_once()
    assert r.call_args[0][1] == "backup-discord-webhook"
    s.assert_called_once()
    slack_mock.assert_not_called()

    webhook_url = s.call_args[0][0]
    embeds = s.call_args.kwargs["embeds"]
    content = s.call_args.kwargs["content"]
    assert webhook_url == "https://discord.com/api/webhooks/1/2"
    assert embeds[0]["title"].startswith("nzshm-backup-lambda-log-errors-prod")
    assert "ALARM" in content
    # Reason appears as a field in the embed
    field_names = [f["name"] for f in embeds[0]["fields"]]
    assert "Reason" in field_names


def test_handler_uses_ok_emoji_on_recovery():
    event = _sns_event(
        {
            "AlarmName": "nzshm-backup-pitr-watcher-errors-prod",
            "NewStateValue": "OK",
            "NewStateReason": "Threshold not crossed",
            "Region": "ap-southeast-2",
            "StateChangeTime": "2026-06-23T01:30:00Z",
        }
    )

    with (
        patch.object(ab, "resolve_webhook_url", return_value="https://x"),
        patch.object(ab, "send_slack") as s,
    ):
        ab.handler(event, None)

    blocks = s.call_args[0][1]
    assert "white_check_mark" in blocks[0]["text"]["text"]


def test_handler_resolves_webhook_once_for_batched_records():
    event = {
        "Records": [
            {
                "Sns": {
                    "Subject": "ALARM",
                    "Message": json.dumps(
                        {"AlarmName": "a", "NewStateValue": "ALARM", "NewStateReason": "r"}
                    ),
                }
            },
            {
                "Sns": {
                    "Subject": "ALARM",
                    "Message": json.dumps(
                        {"AlarmName": "b", "NewStateValue": "ALARM", "NewStateReason": "r"}
                    ),
                }
            },
        ]
    }

    with (
        patch.object(ab, "resolve_webhook_url", return_value="https://x") as r,
        patch.object(ab, "send_slack") as s,
    ):
        result = ab.handler(event, None)

    assert result["body"] == "delivered=2 skipped=0"
    # Secrets Manager is hit only once per invocation regardless of batch size
    assert r.call_count == 1
    assert s.call_count == 2


def test_handler_skips_non_json_message():
    event = {"Records": [{"Sns": {"Subject": "X", "Message": "not json {"}}]}

    with patch.object(ab, "resolve_webhook_url") as r, patch.object(ab, "send_slack") as s:
        result = ab.handler(event, None)

    assert result == {"statusCode": 200, "body": "delivered=0 skipped=1"}
    r.assert_not_called()
    s.assert_not_called()


def test_handler_skips_message_without_alarm_name():
    event = _sns_event({"NotAnAlarm": True, "Foo": "bar"})

    with patch.object(ab, "resolve_webhook_url") as r, patch.object(ab, "send_slack") as s:
        result = ab.handler(event, None)

    assert result["body"] == "delivered=0 skipped=1"
    r.assert_not_called()
    s.assert_not_called()
