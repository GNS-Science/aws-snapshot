"""Notification senders for the daily health report.

Three channels:

- ``slack``: Slack incoming webhook POST with Block Kit message body.
- ``discord``: Discord incoming webhook POST with native rich embeds
  (distinct from Slack-compat ``/slack``-suffixed URLs which are too
  restrictive for the engine's Block Kit payload).
- ``sns``: AWS SNS publish; topic subscribers receive plain-text email.

SES is deliberately NOT used — see ADR-005 (revised) for the rationale.
"""
