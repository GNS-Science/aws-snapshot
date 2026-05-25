"""SNS-based plain-text email delivery for the daily health report.

The daily report Lambda publishes one message per run to a stage-scoped
SNS topic (``nzshm-backup-reports-{stage}``). Confirmed email subscribers
receive the body as an email; the subject line propagates from the
``Subject`` parameter.

SES is deliberately not used — see ADR-005 (revised) for the rationale.
"""

from __future__ import annotations

import logging

import boto3

logger = logging.getLogger(__name__)

# SNS hard limits the Subject to 100 characters; trim with an ellipsis
# if a future formatter ever produces something longer.
_SUBJECT_MAX = 100


class SnsDeliveryError(Exception):
    """Raised when SNS publish fails."""


def publish_report(
    session: boto3.Session,
    topic_arn: str,
    subject: str,
    body: str,
) -> str:
    """Publish a plain-text message to the reports SNS topic.

    Args:
        session: boto3 session in the backup account.
        topic_arn: ARN of the reports SNS topic.
        subject: Email subject line (truncated to 100 chars if needed).
        body: Plain-text message body. No HTML; readable in any mail
              client as-is.

    Returns:
        The MessageId returned by SNS, useful for log correlation.

    Raises:
        SnsDeliveryError: on publish failure.
    """
    if len(subject) > _SUBJECT_MAX:
        subject = subject[: _SUBJECT_MAX - 1] + "…"

    client = session.client("sns")
    try:
        response = client.publish(TopicArn=topic_arn, Subject=subject, Message=body)
    except Exception as e:  # boto3/botocore client errors
        raise SnsDeliveryError(f"SNS publish failed: {e}") from e

    message_id = str(response.get("MessageId", ""))
    logger.info("SNS publish ok (MessageId=%s, TopicArn=%s)", message_id, topic_arn)
    return message_id
