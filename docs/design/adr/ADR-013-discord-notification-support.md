# ADR-013: Discord notification support (peer to Slack)

- Status: Proposed
- Date: 2026-06-27

## Context

The engine has had a single chat notification channel since
[ADR-008](ADR-008-notification-recipients-managed-from-yaml.md):
a Slack incoming-webhook posting Block Kit messages from
`notifications/slack.py`. Daily health reports and CloudWatch alarm
bridges both target it.

Discord is a common operator-side alternative to Slack and was the
preferred channel for the `public-record-backup` deployment. The
obvious first attempt — Discord's `/slack`-compatibility endpoint —
fails: Discord's compat layer accepts only a restricted subset of
Slack Block Kit and rejects the engine's `header` + `context` blocks
with HTTP 403. Reducing the template to the compat subset would
sacrifice the per-source structure the health-report depends on.

Discord's native webhook API accepts richer **embed** objects than
Slack Block Kit does (color-coded sidebar, structured fields,
footer + timestamp), so a native-format integration is both
necessary (to actually post) and beneficial (richer per-source
rendering).

A second, smaller hurdle: Discord's Cloudflare edge blocks urllib's
default `User-Agent` (`Python-urllib/3.10`) with error code 1010. A
documented Discord-format UA is required.

## Decision

Treat Discord as a peer to Slack, not a variant of it:

1. New module `notifications/discord.py` symmetric to
   `notifications/slack.py`: `send_discord(webhook_url, embeds,
   content)`, `resolve_webhook_url(session, secret_id)`,
   `DiscordDeliveryError`. Sets a Discord-format User-Agent header.
2. New `DiscordConfig` model in `config/models.py` mirroring
   `SlackConfig`. `NotificationConfig` gains a `discord` field with
   the same default-on / post-init pattern as `slack`.
3. New `format_discord(HealthReportData)` builder in
   `health_report.py` producing one rich embed with one field per
   source. The Slack builder stays unchanged.
4. `health_report.send()` independently delivers to Slack and Discord
   (each guarded by `<channel>.enabled` in config). `DeliveryResult`
   gains `discord_attempted` / `discord_ok` / `discord_error`.
5. `lambda_alarm_bridge.handler` selects channel via env var
   (`DISCORD_WEBHOOK_SECRET_ID` set → Discord; else Slack). SAM
   template gains a `DiscordWebhookSecretName` parameter.

Webhook URLs stored in Secrets Manager are the plain Discord form
`https://discord.com/api/webhooks/{id}/{token}` — **not** the `/slack`
suffix variant.

## Alternatives considered

- **Use Discord's Slack-compat `/slack` endpoint.** Rejected: the
  engine's existing Block Kit template fails the compat subset
  validation. Stripping `header` + `context` blocks to fit would
  produce a degraded message worse than what Slack users see.
- **Generic webhook abstraction with `format: 'slack' | 'discord'`
  parameter.** Premature consolidation: only two channels exist and
  they have meaningfully different message models. Worth revisiting
  if a third channel (Teams, Mattermost) appears.
- **Per-source channel routing.** Deferred. Currently all sources
  notify to the same channel(s); per-source routing is a separate
  problem that can layer on top.

## Consequences

- Future chat channels follow this same pattern: new module + new
  `XConfig` + new `format_x()` builder + classifier hooks.
- Both channels can run concurrently; deployments mixing Slack and
  Discord (e.g. team Slack + ops Discord) work out of the box.
- The User-Agent rule is a Discord-specific gotcha that operators of
  other webhook-accepting services may encounter — documented in
  `notifications/discord.py`'s module docstring.
- `lambda_alarm_bridge`'s env-var channel selection is coarser than
  the config-driven selection elsewhere. Acceptable because the
  alarm-bridge runs in response to SNS events without reading the
  YAML config, but worth noting as a deliberate asymmetry.

## Implementation history

Already implemented and deployed pre-ADR (a process failure documented
in [#67](https://github.com/GNS-Science/nzshm-backup/issues/67)).
The merged code lives at:

- #61 `085a0b9` — native Discord module + config + integration
- #62 `9fcaa14` — User-Agent fix for Cloudflare 1010
