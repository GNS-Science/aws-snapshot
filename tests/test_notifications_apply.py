"""Tests for `backup notifications apply` (SNS subscription converger)."""

from __future__ import annotations

from unittest.mock import MagicMock

from nzshm_backup.commands.notifications import _diff_subscriptions


def _paginator_with(subs: list[dict]) -> MagicMock:
    sns = MagicMock()
    sns.get_paginator.return_value.paginate.return_value = [{"Subscriptions": subs}]
    return sns


def test_diff_empty_topic_with_one_desired_returns_one_add():
    sns = _paginator_with([])
    diff = _diff_subscriptions(sns, "arn:t", "topic", ["a@example.com"])
    assert diff.to_add == ["a@example.com"]
    assert diff.to_remove == []
    assert diff.kept == []
    assert diff.pending == []


def test_diff_removes_subscription_not_in_desired_list():
    sns = _paginator_with(
        [
            {
                "Protocol": "email",
                "Endpoint": "stale@example.com",
                "SubscriptionArn": "arn:sub:stale",
            }
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", [])
    assert diff.to_add == []
    assert diff.to_remove == [("stale@example.com", "arn:sub:stale")]


def test_diff_keeps_matching_subscription():
    sns = _paginator_with(
        [
            {
                "Protocol": "email",
                "Endpoint": "keep@example.com",
                "SubscriptionArn": "arn:sub:keep",
            }
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", ["keep@example.com"])
    assert diff.kept == ["keep@example.com"]
    assert diff.to_add == []
    assert diff.to_remove == []


def test_diff_handles_mixed_add_remove_keep():
    sns = _paginator_with(
        [
            {
                "Protocol": "email",
                "Endpoint": "keep@example.com",
                "SubscriptionArn": "arn:sub:keep",
            },
            {
                "Protocol": "email",
                "Endpoint": "stale@example.com",
                "SubscriptionArn": "arn:sub:stale",
            },
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", ["keep@example.com", "new@example.com"])
    assert diff.to_add == ["new@example.com"]
    assert diff.to_remove == [("stale@example.com", "arn:sub:stale")]
    assert diff.kept == ["keep@example.com"]


def test_diff_skips_pending_when_email_already_pending_confirmation():
    """Don't re-subscribe an email that's still waiting for confirmation."""
    sns = _paginator_with(
        [
            {
                "Protocol": "email",
                "Endpoint": "still-confirming@example.com",
                "SubscriptionArn": "PendingConfirmation",
            }
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", ["still-confirming@example.com"])
    assert diff.to_add == []
    assert diff.pending == ["still-confirming@example.com"]


def test_diff_ignores_non_email_subscriptions():
    """SQS / HTTP / other protocols on the topic are not our concern."""
    sns = _paginator_with(
        [
            {
                "Protocol": "sqs",
                "Endpoint": "arn:aws:sqs:::queue",
                "SubscriptionArn": "arn:sub:sqs",
            }
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", [])
    assert diff.to_add == []
    assert diff.to_remove == []


def test_diff_is_case_insensitive_for_emails():
    """Email address comparison normalises to lowercase."""
    sns = _paginator_with(
        [
            {
                "Protocol": "email",
                "Endpoint": "Mixed.Case@Example.com",
                "SubscriptionArn": "arn:sub:1",
            }
        ]
    )
    diff = _diff_subscriptions(sns, "arn:t", "topic", ["mixed.case@example.com"])
    assert diff.kept == ["mixed.case@example.com"]
    assert diff.to_add == []
    assert diff.to_remove == []
