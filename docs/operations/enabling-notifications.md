# Enabling notifications (post-merge runbook)

The daily health-report code path is in place but ships **disabled** so
the merge has no operational surface. This runbook walks through the
one-time setup to turn on Slack and/or SNS-email delivery.

There are two channels and they're independent — you can enable one,
the other, or both. Recommended order: Slack first (fastest to test),
SNS email second.

---

## Prerequisites

- `AWS_PROFILE=nshm-backup-admin` SSO session in the backup account
  (`737696831915`).
- Edit access to `backup-config.production.yaml`.
- Permission to deploy via `npx sls deploy --stage prod`.

---

## Channel 1 — Slack webhook

### One-time setup

1. Create a Slack incoming webhook in the GNS workspace pointing at the
   target channel (#nshm-backups or wherever the team agrees).
2. Copy the webhook URL.
3. Store it in AWS Secrets Manager with the name `backup-slack-webhook`
   (matches `notifications.slack.webhook_url_secret` in the config):

   ```bash
   aws secretsmanager create-secret \
     --name backup-slack-webhook \
     --secret-string 'https://hooks.slack.com/services/...your-real-url...' \
     --description 'Slack incoming webhook for NSHM backup health reports'
   ```

### Enable

In `backup-config.production.yaml`:

```yaml
notifications:
  slack:
    enabled: true              # <-- was false
    webhook_url_secret: backup-slack-webhook
    channel: '#nshm-backups'   # informational; webhook routes the channel
```

Push the config update if your project uses `backup config push`, then
no deploy needed for the Slack path — the Lambda reads the config at
invocation time.

### Test

```bash
uv run backup health-report run --send
```

A formatted Block Kit message should appear in the configured channel.
Run output prints `Slack: ok` under the Delivery summary.

---

## Channel 2 — SNS email

### Edit config

In `backup-config.production.yaml`:

```yaml
notifications:
  reports:
    email:
      enabled: true                                # <-- was false
      address: backup-reports@your-team-list.example.com   # <-- was null
```

### Deploy

The SNS subscription is managed by CloudFormation (see
`serverless.yml` → `BackupReportEmailSubscription`), so you do need a
deploy to create or update it:

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
```

CloudFormation creates the topic (`nzshm-backup-reports-prod`) and a
pending subscription for the address above.

### Confirm subscription

AWS sends a "Subscription Confirmation" email from
`no-reply@sns.amazonaws.com` to the configured address. **Click the
Confirm subscription link.** Until you do, the subscription stays in
`PendingConfirmation` state and no daily reports are delivered.

Verify:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-reports-prod
```

The `SubscriptionArn` field should be a real ARN (not the literal
`PendingConfirmation`).

### Test

```bash
uv run backup health-report run --send
```

A plain-text report should appear in the inbox of the configured
address. Run output prints `SNS: ok (MessageId=...)`.

---

## What runs when

| Command | What it does | Hits AWS? |
|---|---|---|
| `backup health-report preview` | Skip restore tests, print only | Yes (status + Athena counts + inventory listing) |
| `backup health-report run` | Full report (incl. restore tests), print only | Yes (everything; ~30s + ~30s per source restore-tested) |
| `backup health-report run --send` | As above, also delivers via configured channels | Yes |
| `backup health-report run --weekday N` | Force rotation for that weekday (0=Mon … 6=Sun) | Yes |

PR B will add the daily EventBridge schedule that fires
`backup health-report run --send` automatically at 14:30 NZST.

---

## Subscribing additional recipients

For the SNS path: add another subscription to the reports topic via the
AWS console or CLI. No code change needed.

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-reports-prod \
  --protocol email \
  --notification-endpoint another-person@example.com
# AWS emails them a confirmation link; they click it.
```

For Slack: add the webhook to another channel via the Slack app
configuration, or post the same message to multiple channels by storing
multiple webhook URLs (currently the code only resolves one — extend
the senders if this is needed).

---

## Disabling

Reverse the flow:

- Slack: set `notifications.slack.enabled: false` in the config and
  refresh.
- SNS email: set `notifications.reports.email.enabled: false` and
  redeploy — CloudFormation will remove the email subscription. The
  topic itself stays so any other subscribers (e.g., separately added
  via the CLI above) are not affected.

---

## Why not SES?

ADR-005 originally specified SES for HTML email. Reviewed and rejected
on 2026-05-20 — see ADR-005's revision note. Summary: SES requires
domain verification, IAM scoping to a sending identity, and possibly a
sandbox-mode escape ticket. For a daily ops report sent to a small
internal audience, SNS-driven plain-text email is sufficient and ships
with no DNS prerequisites.

---

## Related

- ADR-005 (revised) — design rationale
- ADR-006 mit. 1 — object-count delta check (folded into the report)
- ADR-007 mit. 4 — inventory freshness watchdog (folded into the report)
- `docs/operations/inventory-bucket-recovery.md` — sibling runbook
