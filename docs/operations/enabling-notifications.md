# Managing notification recipients

Both notification channels (CloudWatch alarm → SNS, daily report → SNS)
are driven by lists in `backup-config.production.yaml`. The
`backup notifications apply` command reconciles each topic's actual SNS
subscriptions to match the YAML.

Single source of truth → no double-bookkeeping between repo and AWS.

There's also a Slack channel (incoming webhook) which is a one-time
setup, separate from the SNS list workflow.

---

## Prerequisites

- `AWS_PROFILE=nshm-backup-admin` SSO session in the backup account
  (`737696831915`).
- Edit access to `backup-config.production.yaml`.
- Permission to deploy via `npx sls deploy --stage prod` (only needed
  for code/CloudFormation changes — recipient changes do not require a
  deploy).

---

## Adding / removing recipients (SNS email channels)

### 1. Edit the lists in `backup-config.production.yaml`

```yaml
notifications:
  alerts:
    emails:
      - oncall1@example.com         # add/remove freely
      - oncall2@example.com
  reports:
    email:
      enabled: true                 # gate the daily report channel
      addresses:
        - reports-list@example.com
        - me@example.com
```

The two lists are independent — somebody can be on alerts but not
reports, or vice versa.

### 2. Apply

```bash
uv run backup notifications apply
```

The command:

1. Reads both lists from YAML.
2. Lists the actual SNS subscriptions on `nzshm-backup-alerts-prod`
   and `nzshm-backup-reports-prod`.
3. Computes the diff per topic.
4. Calls `sns:Subscribe` for new addresses, `sns:Unsubscribe` for
   removed ones.
5. Prints a per-channel summary plus a per-email line: `+` added,
   `-` removed, `=` kept, `~` still pending confirmation.

Example output:

```
[alerts] nzshm-backup-alerts-prod
  topic: arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-alerts-prod
  desired=2  current=2  add=1  remove=1  pending=0
  + subscribed oncall2@example.com  (awaiting confirmation email)
  - unsubscribed old-oncall@example.com
  = oncall1@example.com

[reports] nzshm-backup-reports-prod
  ...

Subscriptions updated. New subscribers must click the confirmation
email link before delivery starts.
```

### 3. New subscribers click the confirmation link

Anyone newly subscribed receives a "Subscription Confirmation" email
from `no-reply@sns.amazonaws.com`. They click the link inside.
Until they do, their `SubscriptionArn` reads `PendingConfirmation`
and they get no real messages. Re-running `apply` for a
still-pending address is a no-op — AWS doesn't let you re-issue the
confirmation; either it gets clicked, or the pending subscription
expires after ~3 days and a fresh `apply` will create a new one.

### 4. Inspect current state without changing anything

```bash
uv run backup notifications show
```

Lists every email subscription on both topics with its `confirmed` /
`pending` state. Useful for sanity-checking after edits.

### 5. Dry-run before applying

```bash
uv run backup notifications apply --dry-run
```

Prints exactly what `apply` would do without calling Subscribe /
Unsubscribe. Helpful for reviewing a large list change.

### Filter to one channel

```bash
uv run backup notifications apply --only reports
uv run backup notifications apply --only alerts
```

---

## Live verify after a recipient change

```bash
# Force the alarm to test the alerts path
uv run backup test alert

# Build + deliver the daily report to exercise the reports path
uv run backup health-report run --send
```

Both should land in the inbox of every confirmed subscriber on the
relevant list.

---

## Slack channel (separate from SNS)

Slack delivery is a single webhook, not a list — the membership of the
target channel is managed inside Slack. One-time setup:

### Create the webhook

1. https://api.slack.com/apps → "Create New App" → "From scratch"
2. Choose workspace, name the app (e.g. "NSHM Backup Reports").
3. **Incoming Webhooks** → toggle On.
4. **Add New Webhook to Workspace** → pick the channel
   (e.g. `#cwg-automata-notices`).
5. Copy the Webhook URL — it is the credential. Treat like a password.

### Store in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name backup-slack-webhook \
  --secret-string 'https://hooks.slack.com/services/...your-real-url...'
```

(If the secret already exists, use `put-secret-value --secret-id`.)

### Enable

```yaml
notifications:
  slack:
    enabled: true                    # was false
    webhook_url_secret: backup-slack-webhook
    channel: '#cwg-automata-notices' # informational only
```

`backup config push --stage prod` if you maintain the SSM-stored copy.
Lambda picks up the change on the next invocation — no
`sls deploy` required.

### Verify

```bash
uv run backup health-report run --send
```

Expect `Slack: ok` in the Delivery summary.

### Rotating the webhook

If the webhook URL ever leaks, regenerate it in the Slack app UI then:

```bash
aws secretsmanager put-secret-value \
  --secret-id backup-slack-webhook \
  --secret-string 'https://hooks.slack.com/services/<new>'
```

No further action needed.

---

## Disabling

To temporarily snooze one channel:

```yaml
notifications:
  slack:
    enabled: false       # Lambda stops posting; subscriptions untouched
  reports:
    email:
      enabled: false     # Lambda stops publishing to reports topic
```

`backup notifications apply` does not change subscriptions when the
channel is disabled — it still reads the lists. To clear the lists
when disabling permanently:

```yaml
notifications:
  alerts:
    emails: []
  reports:
    email:
      enabled: false
      addresses: []
```

Then `backup notifications apply` removes everyone from the topics.

---

## Why this design (SES rejected, not used)

ADR-005 originally specified SES for HTML email. Rejected after review:
SES requires domain verification, a sandbox-mode escape ticket, and
tight IAM scoping to a sending identity — significant ops prerequisites
for a daily report sent to a small internal audience. SNS email
subscription is the same subscribe-and-confirm flow whether the recipient
is one person or a mailing list, with no DNS prerequisites.

---

## Related

- ADR-005 (revised) — design rationale
- ADR-006 mit. 1 — object-count delta check
- ADR-007 mit. 4 — freshness watchdog
- `docs/user-guide/health-report.md` — operator-facing report walkthrough
- `docs/operations/inventory-bucket-recovery.md` — sibling runbook
