"""Discord incoming-webhook delivery for the daily health report.

Uses Discord's **native** webhook format with rich embeds — distinct from
the Slack-compat endpoint (URL ending in ``/slack``) which only accepts
a restricted subset of Slack Block Kit. Native embeds support
colour-coded sidebar, structured fields, footer, and timestamps, which
render the per-source breakdown better than the Slack-compat path.

Webhook URL format expected: the standard Discord webhook URL **without**
a ``/slack`` suffix, i.e. ``https://discord.com/api/webhooks/{id}/{token}``.

Uses ``urllib.request`` to avoid pulling ``requests`` into the Lambda
package for a single POST. The webhook URL is retrieved from AWS Secrets
Manager by the caller (separately from this module so the secret never
round-trips through anything that logs request bodies).
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

# Discord embed colour palette (decimal RGB).
COLOUR_GREEN = 0x2ECC71
COLOUR_AMBER = 0xE67E22
COLOUR_RED = 0xE74C3C
COLOUR_BLUE = 0x3498DB


class DiscordDeliveryError(Exception):
    """Raised when Discord returns a non-2xx response or the request fails."""


def resolve_webhook_url(session: boto3.Session, secret_id: str) -> str:
    """Fetch the Discord webhook URL from Secrets Manager.

    Args:
        session: boto3 session in the backup account.
        secret_id: Secrets Manager secret name (e.g. ``backup-discord-webhook``).
    """
    client = session.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    return str(response["SecretString"]).strip()


def send_discord(
    webhook_url: str,
    embeds: list[dict[str, Any]] | None = None,
    content: str | None = None,
    username: str | None = None,
) -> None:
    """POST a message to a Discord incoming webhook.

    Args:
        webhook_url: Resolved Discord webhook URL (no ``/slack`` suffix).
        embeds: Up to 10 embed objects (Discord cap). Each embed is a dict
            with optional ``title``, ``description``, ``color``,
            ``fields``, ``footer``, ``timestamp``, ``url``, ``author``.
            See https://discord.com/developers/docs/resources/webhook.
        content: Plain-text body shown above any embeds. Up to 2000 chars.
            At least one of ``content`` or ``embeds`` must be set.
        username: Override the webhook's default display name.

    Raises:
        DiscordDeliveryError: on HTTP error or non-2xx response.
        ValueError: if neither content nor embeds is provided.
    """
    if not content and not embeds:
        raise ValueError("Either content or embeds must be provided")

    payload: dict[str, Any] = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    if username:
        payload["username"] = username

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            # urllib's default User-Agent ("Python-urllib/3.x") is blocked by
            # Discord's Cloudflare edge with error code 1010. Use a custom UA
            # following Discord's documented format
            # (https://discord.com/developers/docs/reference#user-agent).
            "User-Agent": "aws-snapshot (https://github.com/GNS-Science/nzshm-backup, 0.1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise DiscordDeliveryError(
            f"Discord webhook returned HTTP {e.code}: {e.reason}: {body[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise DiscordDeliveryError(f"Discord webhook unreachable: {e.reason}") from e

    if status < 200 or status >= 300:
        raise DiscordDeliveryError(f"Discord webhook returned HTTP {status}: {body[:200]}")

    logger.info("Discord delivery ok (status=%d)", status)
