# ADR-005: Automated daily health report + Lambda error alarm

- Status: Accepted (fast path shipped 2026-05-19; slow path shipped 2026-05-20)
- Date: 2026-05-12 (revised 2026-05-19, 2026-05-20)

> **2026-05-20 revision — SES dropped in favour of SNS email.** ADR-005's
> original design specified SES for HTML email delivery of the daily
> report. On review, SES was rejected: it requires domain verification,
> tight IAM scoping to a sending identity, and possibly a sandbox-mode
> escape ticket — significant ops prerequisites for a daily ops report
> sent to a small internal audience. The slow path now delivers email
> via SNS (separate `nzshm-backup-reports-{stage}` topic, plain-text
> body with a status-bearing subject line). Slack delivery is unchanged
> and remains the rich-formatting channel. See
> `docs/operations/enabling-notifications.md` for the post-merge runbook.

## Context

The backup system runs daily across four production sources (50.8M objects,
11 TB). Health validation currently requires a human to run CLI commands
(`backup status`, `test restore`, `test integrity`) and interpret the output.

On 2026-05-19 we discovered that the weka source had been failing every
scheduled run since 2026-05-15 (Athena UNLOAD `HIVE_PATH_ALREADY_EXISTS`
race against a leftover manifest file). The error logged at ERROR level
on every invocation but no one was notified, so four daily runs failed
silently. This incident drives two requirements:

1. **Immediate notification** when a scheduled invocation fails — measured
   in minutes, not days.
2. **Scheduled aggregate visibility** so silent issues (stale inventory,
   zero-row UNLOAD when diffs were expected, drifting object counts) get
   surfaced even when no Lambda error fires.

The notification config infrastructure already exists in
`backup-config.production.yaml` (Slack webhook + SES) but is disabled and
unimplemented (Phase 3 of the project plan).

## Decision

Two complementary notification paths:

### Fast path — CloudWatch alarm on Lambda errors

CloudWatch alarm on the `nzshm-backup-service-prod-backup` Lambda's
`Errors` metric (threshold ≥ 1 over a 5-minute period) → SNS topic →
Slack webhook subscription + email subscription. Fires within minutes
of any failed invocation. Narrow scope: catches hard errors only, not
silent issues.

### Slow path — daily health report

Extend the existing backup Lambda to accept a `health_report` task type,
triggered daily by a dedicated EventBridge rule (after the backup runs
complete). The report combines status checks and restore verification
into a single formatted message sent via the same Slack webhook and SES
email destinations.

### Report contents

1. **Daily run summary** — for each source: last run status (skipped/submitted/
   failed), inventory freshness, latest batch job result
2. **Restore verification** — `test restore` on weka every day (canary,
   checksum verified) plus one large source rotating through the week
   (Mon ths → Wed toshi → Fri static), so every source gets a verified
   restore at least weekly
3. **DynamoDB PITR status** — for toshi: all tables PITR enabled, export
   bucket accessible
4. **Object count summary** — source vs backup counts from inventory

### Architecture

```
                                 ┌──── SNS topic ──── Slack webhook
Lambda Errors metric ── alarm ──┤                 └── email
                                 │
EventBridge (daily, ~14:00 NZST) │
    │                            │
    ▼                            │
Lambda (existing backup function)│
    │  event: {"task": "health_report"}
    │
    ├── run_health_check()        ← new module
    │   ├── status checks (programmatic, reuse backup_engine)
    │   ├── test restore (reuse sample_objects_via_inventory + verify)
    │   └── format report
    │
    ├── send_slack_webhook()      ← new module
    │   └── POST to webhook URL from Secrets Manager
    │
    └── send_ses_email()          ← new module
        └── SES SendEmail with HTML body
```

### Why extend the existing Lambda (not a new one)

- Reuses existing IAM role (S3, Athena, Glue, STS, DynamoDB permissions)
- Reuses existing config loading from SSM
- Reuses existing cross-account session management
- Single deployment artifact — `sls deploy` updates everything
- EventBridge rule management via existing `backup schedule` CLI

### Delivery format

**Slack:** Block Kit message with sections for each source, emoji status
indicators (✓/✗/⋯), and a summary line.

**Email:** HTML table with the same data, suitable for archiving. Sent via
SES from `noreply-backup@<domain>` to configured recipients.

## Implementation scope

| Component | File | Effort |
|-----------|------|--------|
| CloudWatch alarm + SNS topic | `serverless.yml` (resources block) | Small |
| SNS → Slack subscription | Lambda subscriber or AWS Chatbot | Small |
| SNS → email subscription | `serverless.yml` (manual confirm step) | Trivial |
| Health check runner | `src/nzshm_backup/health_report.py` (new) | Medium |
| Slack sender | `src/nzshm_backup/notifications/slack.py` (new) | Small |
| SES sender | `src/nzshm_backup/notifications/ses.py` (new) | Small |
| Lambda handler extension | `src/nzshm_backup/lambda_handler.py` | Small |
| EventBridge rule | `backup schedule add --source health --frequency daily` | Trivial |
| Config enablement | `backup-config.production.yaml` notifications section | Trivial |
| SES domain verification | AWS console / CLI (one-time) | Small |
| Slack webhook setup | Slack app config + Secrets Manager (one-time) | Small |

### Prerequisites

- SES: verify a sending domain or email address in the backup account
- Slack: create an incoming webhook and store the URL in Secrets Manager
  (key: `backup-slack-webhook`, already referenced in config)
- Lambda IAM: add `ses:SendEmail`, `secretsmanager:GetSecretValue`
  to `serverless.yml`

## Alternatives considered

1. **Separate Lambda** — cleaner separation but duplicates IAM role, config
   loading, and cross-account session setup. More deployment surface.
2. **CodeBuild + CLI** — runs the actual CLI commands and pipes output.
   Simpler but not serverless, harder to format for Slack/email.
3. **CloudWatch alarm alone** — fast but narrow: only catches hard Lambda
   errors, misses silent issues (stale inventory, zero-row UNLOAD when
   diffs were expected, drift). Adopted as the fast path *alongside* the
   daily report, not as an alternative.
4. **Weekly report only** (original proposal) — rejected after the
   2026-05-19 weka incident demonstrated that a 7-day notification gap
   is unacceptable for a system running daily.

## Risks

- **Lambda timeout**: health check + restore tests must complete within
  15 minutes. Current timing: ~30 seconds per source for restore tests,
  ~10 seconds for status. Total ~2 minutes — well within limits.
- **SES sending limits**: new SES accounts start in sandbox mode (verified
  recipients only). Need to request production access if sending to
  external addresses.
- **Slack webhook rotation**: webhook URLs don't expire but can be
  revoked. Storing in Secrets Manager allows rotation without redeploying.

## Links

- Notification config: `backup-config.production.yaml` → `notifications`
- Phase 3 (Notifications + Reporting): `docs/design/backup-solution-plan.md`
- Validation strategy: `docs/design/VALIDATION_STRATEGY.md`
