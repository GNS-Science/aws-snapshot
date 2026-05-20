"""Slack incoming-webhook delivery for the daily health report.

Uses ``urllib.request`` to avoid pulling ``requests`` into the Lambda
package for a single POST. The webhook URL is retrieved from AWS
Secrets Manager by the caller (separately from this module so the
secret never round-trips through anything that logs request bodies).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


class SlackDeliveryError(Exception):
    """Raised when Slack returns a non-2xx response or the request fails."""


def resolve_webhook_url(session: boto3.Session, secret_id: str) -> str:
    """Fetch the Slack webhook URL from Secrets Manager.

    Args:
        session: boto3 session in the backup account.
        secret_id: Secrets Manager secret name (e.g. ``backup-slack-webhook``).
    """
    client = session.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    # SecretString contains the raw URL — no JSON wrapper expected.
    return str(response["SecretString"]).strip()


def send_slack(webhook_url: str, blocks: list[dict[str, Any]], text: str = "") -> None:
    """POST a Block Kit message to a Slack incoming webhook.

    Args:
        webhook_url: Resolved webhook URL (see ``resolve_webhook_url``).
        blocks: Block Kit ``blocks`` array.
        text: Fallback plain-text summary shown in mobile push / unfurled
              previews. Slack treats ``text`` as required even when
              ``blocks`` is the rich body.

    Raises:
        SlackDeliveryError: on HTTP error or non-2xx response.
    """
    payload = json.dumps({"text": text or "Backup health report", "blocks": blocks}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise SlackDeliveryError(f"Slack webhook returned HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise SlackDeliveryError(f"Slack webhook unreachable: {e.reason}") from e

    if status < 200 or status >= 300:
        raise SlackDeliveryError(f"Slack webhook returned HTTP {status}: {body}")

    logger.info("Slack delivery ok (status=%d)", status)
