"""SNS -> Slack bridge for CloudWatch alarm notifications.

Subscribed to BackupAlertsTopic. Each invocation receives one or more
CloudWatch alarm state-change events (wrapped in an SNS event envelope);
this handler decodes the payload and posts a Block Kit notification to
the Slack channel via the existing backup-slack-webhook secret.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from nzshm_backup.notifications.slack import resolve_webhook_url, send_slack

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_DEFAULT_SECRET_ID = "backup-slack-webhook"

_STATE_EMOJI = {
    "ALARM": ":rotating_light:",
    "OK": ":white_check_mark:",
    "INSUFFICIENT_DATA": ":grey_question:",
}


def _build_blocks(alarm: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    name = alarm.get("AlarmName", "(unknown alarm)")
    new_state = alarm.get("NewStateValue", "(unknown)")
    reason = alarm.get("NewStateReason", "")
    region = alarm.get("Region", "")
    timestamp = alarm.get("StateChangeTime", "")

    emoji = _STATE_EMOJI.get(new_state, ":bell:")
    text = f"{emoji} {name} → {new_state}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": text, "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:* {reason}"},
        },
    ]
    context_text = " • ".join(p for p in (region, timestamp) if p)
    if context_text:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context_text}],
            }
        )
    return blocks, text


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    session = boto3.Session()
    secret_id = os.environ.get("SLACK_WEBHOOK_SECRET_ID", _DEFAULT_SECRET_ID)

    delivered = 0
    skipped = 0
    webhook_url: str | None = None

    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        raw_message = sns.get("Message", "")
        try:
            alarm = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("Skipping non-JSON SNS message subject=%r", sns.get("Subject"))
            skipped += 1
            continue

        if "AlarmName" not in alarm:
            logger.warning("Skipping SNS message without AlarmName: keys=%s", list(alarm.keys()))
            skipped += 1
            continue

        blocks, text = _build_blocks(alarm)

        if webhook_url is None:
            webhook_url = resolve_webhook_url(session, secret_id)

        send_slack(webhook_url, blocks, text=text)
        delivered += 1

    return {"statusCode": 200, "body": f"delivered={delivered} skipped={skipped}"}
