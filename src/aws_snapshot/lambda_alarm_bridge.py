"""SNS -> Slack/Discord bridge for CloudWatch alarm notifications.

Subscribed to BackupAlertsTopic. Each invocation receives one or more
CloudWatch alarm state-change events (wrapped in an SNS event envelope);
this handler decodes the payload and posts a notification to whichever
chat channel is configured via the environment.

Channel selection:
- ``DISCORD_WEBHOOK_SECRET_ID`` set → Discord native embed
- else ``SLACK_WEBHOOK_SECRET_ID`` set → Slack Block Kit
- else fallback to legacy ``SLACK_WEBHOOK_SECRET_ID`` default

This keeps Slack the default for installs that haven't migrated, while
letting Discord-first installs (e.g. public-record-backup) opt in via a
single env-var override on the SAM stack.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from aws_snapshot.notifications.discord import (
    COLOUR_BLUE,
    COLOUR_GREEN,
    COLOUR_RED,
    send_discord,
)
from aws_snapshot.notifications.discord import (
    resolve_webhook_url as resolve_discord_webhook_url,
)
from aws_snapshot.notifications.slack import resolve_webhook_url, send_slack

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_DEFAULT_SLACK_SECRET_ID = "backup-slack-webhook"

_STATE_EMOJI = {
    "ALARM": ":rotating_light:",
    "OK": ":white_check_mark:",
    "INSUFFICIENT_DATA": ":grey_question:",
}

_STATE_COLOUR = {
    "ALARM": COLOUR_RED,
    "OK": COLOUR_GREEN,
    "INSUFFICIENT_DATA": COLOUR_BLUE,
}


def _build_slack_blocks(alarm: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
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


def _build_discord_embed(alarm: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    name = alarm.get("AlarmName", "(unknown alarm)")
    new_state = alarm.get("NewStateValue", "(unknown)")
    reason = alarm.get("NewStateReason", "")
    region = alarm.get("Region", "")
    timestamp = alarm.get("StateChangeTime", "")

    title = f"{name} → {new_state}"
    fields = [
        {"name": "Reason", "value": reason[:1024] if reason else "_(no reason)_", "inline": False}
    ]
    if region:
        fields.append({"name": "Region", "value": region, "inline": True})
    if timestamp:
        fields.append({"name": "Time", "value": timestamp, "inline": True})

    embed = {
        "title": title,
        "color": _STATE_COLOUR.get(new_state, COLOUR_BLUE),
        "fields": fields,
    }
    return [embed], title


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    session = boto3.Session()
    discord_secret_id = os.environ.get("DISCORD_WEBHOOK_SECRET_ID")
    slack_secret_id = os.environ.get("SLACK_WEBHOOK_SECRET_ID", _DEFAULT_SLACK_SECRET_ID)

    delivered = 0
    skipped = 0
    slack_webhook: str | None = None
    discord_webhook: str | None = None

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

        if discord_secret_id:
            embeds, content = _build_discord_embed(alarm)
            if discord_webhook is None:
                discord_webhook = resolve_discord_webhook_url(session, discord_secret_id)
            send_discord(discord_webhook, embeds=embeds, content=content)
        else:
            blocks, text = _build_slack_blocks(alarm)
            if slack_webhook is None:
                slack_webhook = resolve_webhook_url(session, slack_secret_id)
            send_slack(slack_webhook, blocks, text=text)
        delivered += 1

    return {"statusCode": 200, "body": f"delivered={delivered} skipped={skipped}"}
