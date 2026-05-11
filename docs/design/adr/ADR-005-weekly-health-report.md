# ADR-005: Automated weekly health report via Slack + email

- Status: Proposed
- Date: 2026-05-12

## Context

The backup system runs daily across four production sources (50.8M objects,
11 TB). Health validation currently requires a human to run CLI commands
(`backup status`, `test restore`, `test integrity`) and interpret the output.

We want a weekly automated health report sent to a Slack channel and email
address so the team has visibility without manual intervention.

The notification config infrastructure already exists in
`backup-config.production.yaml` (Slack webhook + SES) but is disabled and
unimplemented (Phase 3 of the project plan).

## Decision

Extend the existing backup Lambda to accept a `health_report` task type,
triggered weekly by a dedicated EventBridge rule. The report combines
status checks and restore verification into a single formatted message
sent via Slack webhook and SES email.

### Report contents

1. **Daily run summary** — for each source: last run status (skipped/submitted/
   failed), inventory freshness, latest batch job result
2. **Restore verification** — `test restore` on weka (canary, checksum verified)
   and one large source (rotating weekly: ths → toshi → static)
3. **DynamoDB PITR status** — for toshi: all tables PITR enabled, export
   bucket accessible
4. **Object count summary** — source vs backup counts from inventory

### Architecture

```
EventBridge (weekly, e.g. Friday 14:00 NZST)
    │
    ▼
Lambda (existing backup function)
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
| Health check runner | `src/nzshm_backup/health_report.py` (new) | Medium |
| Slack sender | `src/nzshm_backup/notifications/slack.py` (new) | Small |
| SES sender | `src/nzshm_backup/notifications/ses.py` (new) | Small |
| Lambda handler extension | `src/nzshm_backup/lambda_handler.py` | Small |
| EventBridge rule | `backup schedule add --source health --frequency weekly` | Trivial |
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
3. **CloudWatch Alarm + SNS** — monitors Lambda errors/invocations only.
   No restore verification, no object counts, no formatted report.

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
