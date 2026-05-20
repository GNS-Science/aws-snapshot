"""Notification senders for the daily health report.

Two channels:

- ``slack``: incoming webhook POST with Block Kit message body.
- ``sns``: AWS SNS publish; topic subscribers receive plain-text email.

SES is deliberately NOT used — see ADR-005 (revised) for the rationale.
"""
