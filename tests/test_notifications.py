"""Tests for notification senders (Slack webhook + SNS publish)."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from nzshm_backup.notifications import slack, sns

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def _fake_urlopen_response(status: int = 200, body: bytes = b"ok"):
    """Minimal context-manager mock matching urllib's urlopen() return."""
    mock = MagicMock()
    mock.__enter__.return_value.status = status
    mock.__enter__.return_value.read.return_value = body
    return mock


def test_send_slack_posts_blocks_with_text_fallback():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        return _fake_urlopen_response()

    with patch.object(slack.urllib.request, "urlopen", side_effect=fake_urlopen):
        slack.send_slack("https://hooks.slack.com/services/X/Y/Z", blocks, text="fallback")

    assert captured["url"] == "https://hooks.slack.com/services/X/Y/Z"
    assert captured["headers"].get("Content-type") == "application/json"
    payload = json.loads(captured["body"])
    assert payload["blocks"] == blocks
    assert payload["text"] == "fallback"


def test_send_slack_uses_default_text_when_omitted():
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        return _fake_urlopen_response()

    with patch.object(slack.urllib.request, "urlopen", side_effect=fake_urlopen):
        slack.send_slack("https://hooks.slack.com/x", [{"type": "section"}])

    payload = json.loads(captured["body"])
    assert payload["text"] == "Backup health report"


def test_send_slack_raises_on_http_error():
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "Internal Error", {}, io.BytesIO(b""))

    with patch.object(slack.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(slack.SlackDeliveryError, match="HTTP 500"):
            slack.send_slack("https://hooks.slack.com/x", [])


def test_send_slack_raises_on_url_error():
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    with patch.object(slack.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(slack.SlackDeliveryError, match="unreachable"):
            slack.send_slack("https://hooks.slack.com/x", [])


def test_resolve_webhook_url_fetches_from_secrets_manager():
    session = MagicMock()
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {
        "SecretString": "https://hooks.slack.com/services/AAA/BBB/CCC\n"
    }
    session.client.return_value = secrets

    url = slack.resolve_webhook_url(session, "backup-slack-webhook")

    assert url == "https://hooks.slack.com/services/AAA/BBB/CCC"
    session.client.assert_called_once_with("secretsmanager")
    secrets.get_secret_value.assert_called_once_with(SecretId="backup-slack-webhook")


# ---------------------------------------------------------------------------
# SNS
# ---------------------------------------------------------------------------


def test_publish_report_calls_sns_with_expected_args():
    session = MagicMock()
    sns_client = MagicMock()
    sns_client.publish.return_value = {"MessageId": "msg-123"}
    session.client.return_value = sns_client

    msg_id = sns.publish_report(
        session,
        "arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-reports-prod",
        subject="health 2026-05-20 GREEN",
        body="body text",
    )

    assert msg_id == "msg-123"
    session.client.assert_called_once_with("sns")
    sns_client.publish.assert_called_once_with(
        TopicArn="arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-reports-prod",
        Subject="health 2026-05-20 GREEN",
        Message="body text",
    )


def test_publish_report_truncates_long_subject():
    session = MagicMock()
    sns_client = MagicMock()
    sns_client.publish.return_value = {"MessageId": "x"}
    session.client.return_value = sns_client

    long_subject = "a" * 150
    sns.publish_report(session, "arn:aws:sns:::topic", subject=long_subject, body="body")

    kwargs = sns_client.publish.call_args.kwargs
    assert len(kwargs["Subject"]) == 100
    assert kwargs["Subject"].endswith("…")


def test_publish_report_raises_on_boto_error():
    session = MagicMock()
    sns_client = MagicMock()
    sns_client.publish.side_effect = RuntimeError("AccessDenied")
    session.client.return_value = sns_client

    with pytest.raises(sns.SnsDeliveryError, match="AccessDenied"):
        sns.publish_report(session, "arn:aws:sns:::topic", subject="s", body="b")
