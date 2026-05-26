# Operator Cheatsheet

The whole production setup has four moving parts that change
independently. This table maps "I want to change X" to "do Y".

| To change | Edit | Then run | Need `sls deploy`? |
|---|---|---|---|
| **Who gets alarm emails** | `notifications.alerts.emails` list in `backup-config.production.yaml` | `backup notifications apply` | No |
| **Who gets daily-report emails** | `notifications.reports.email.addresses` list | `backup notifications apply` | No |
| **Slack channel destination** | Re-create webhook in Slack pointing at new channel | `aws secretsmanager put-secret-value --secret-id backup-slack-webhook --secret-string '<new URL>'` | No |
| **Health-report thresholds** (canary, rotation, freshness, delta, sample size) | `notifications.reports.health` block | `backup config push --stage prod` | No |
| **Schedule cadence/time** (backup or health report) | n/a — EventBridge lives outside config | `backup schedule remove --task-type ... --frequency daily` then `add` with new `--time` | No |
| **Source-bucket list, retention, IAM, anything else in YAML** | `backup-config.production.yaml` | `backup config push --stage prod` | No |
| **Lambda code, IAM permissions, CFN resources, SNS topics** | source files / `serverless.yml` | `make check`, commit | **Yes** (`AWS_PROFILE=<aws-profile> npx sls deploy --stage prod`) |

---

## Standard prefix for live AWS commands

```bash
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"
```

All commands below assume this is in your shell. Re-run if you get
`Token has expired` (SSO sessions are 8h).

---

## Daily operational checks

```bash
# What's the system doing right now?
uv run backup status

# Was today's backup OK?
uv run backup health-report preview            # fast (no restore tests)
uv run backup health-report run                # full (~2 min)

# Did the schedules actually fire?
aws events list-rules --name-prefix nzshm-backup
```

---

## Modifying recipients (most common change)

```bash
# 1. Edit the lists
$EDITOR backup-config.production.yaml

# 2. Apply (preview first if it's a big change)
uv run backup notifications apply --dry-run
uv run backup notifications apply

# 3. Verify state
uv run backup notifications show
```

New subscribers click an AWS confirmation email link.
Removed subscribers stop receiving messages immediately.

---

## Modifying health-report thresholds

```bash
# 1. Edit notifications.reports.health in YAML
$EDITOR backup-config.production.yaml

# 2. Validate + push to SSM (Lambda reads from SSM)
uv run backup config validate
BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup config push --stage prod

# 3. Verify by running the report manually
uv run backup health-report preview
```

The new thresholds take effect on the next Lambda invocation
(scheduled or manual). No restart, no deploy.

---

## Validating health-classification changes end-to-end

When you change `_classify_source` logic, add a new signal, or want to
verify ADR-009 claims against real AWS infrastructure, set up the two
sandbox toy sources and walk the scenario matrix:

```bash
# Runbook covers source-bucket creation, IAM, Inventory wiring,
# scenario actions, expected per-row signals, and full teardown.
$EDITOR docs/operations/health-signal-validation-sandbox.md
```

Full sweep is ~48h (two S3 Inventory cycles); scenarios stack so
4+ signals are exercised per cycle. The `toy-noinv` source uses
`inventory_enabled: false` to exercise the no-Inventory floor.

---

## Changing a schedule

EventBridge rules are AWS resources, not YAML. Use the `schedule` CLI:

```bash
# Move the daily health report from 14:30 to 15:00 NZST
uv run backup schedule remove --source _health --task-type health_report --frequency daily
uv run backup schedule add    --source _health --task-type health_report \
    --frequency daily --time "15:00 NZST"

# Snooze without deleting
uv run backup schedule disable --source _health --task-type health_report --frequency daily

# Re-enable
uv run backup schedule enable --source _health --task-type health_report --frequency daily

# What's currently scheduled?
uv run backup schedule show
```

For per-source backup rules, drop the `--task-type` flag (defaults to
`backup`) and pass the real source alias:

```bash
uv run backup schedule add --source toshi --frequency daily --time "13:05 NZST"
```

---

## Shipping code changes

```bash
# 1. Local verification
make check                                          # lint + mypy + pytest

# 2. Push branch + open PR (standard git workflow)
git push -u origin <branch>
gh pr create --base pre-release ...

# 3. After review/merge — deploy
AWS_PROFILE=<aws-profile> npx sls deploy --stage prod

# 4. Smoke test
uv run backup health-report run --send
uv run backup test alert
```

---

## When something looks wrong

| Symptom | First check |
|---|---|
| Daily report shows red for one source | The investigate-by-signal table in [user-guide/health-report.md](../user-guide/health-report.md#when-you-see-red--investigate-by-signal-table) |
| Daily report didn't arrive at 14:30 NZST | `aws events list-rules --name-prefix nzshm-backup-health-report-daily` — is it `ENABLED`?  CloudWatch logs for the backup Lambda around 02:30 UTC. |
| Alarm fired but no email | `backup notifications show` — is the subscription `confirmed` not `pending`?  Spam folder?  Topic deleted? |
| Inventory bucket gone / corrupted | [operations/inventory-bucket-recovery.md](inventory-bucket-recovery.md) |
| Athena UNLOAD `HIVE_PATH_ALREADY_EXISTS` | Issue #18 — known race condition; delete the leftover under `_manifests/unload/<source>/<bucket>/` and re-run |

---

## Quick reference: ARNs and bucket names

| Resource | ARN/Name |
|---|---|
| Backup Lambda | `arn:aws:lambda:ap-southeast-2:123456789012:function:nzshm-backup-service-prod-backup` |
| Alerts SNS topic | `arn:aws:sns:ap-southeast-2:123456789012:nzshm-backup-alerts-prod` |
| Reports SNS topic | `arn:aws:sns:ap-southeast-2:123456789012:nzshm-backup-reports-prod` |
| Lambda-error alarm | `nzshm-backup-lambda-errors-prod` |
| Health-report rule | `nzshm-backup-health-report-daily` |
| Inventory bucket | `nzshm-backup-inventory-123456789012` |
| Backup buckets | `bb-{source}-s3-{label}-ap-southeast-2-210987654321` (per source) |
| Slack webhook secret | Secrets Manager: `backup-slack-webhook` |
| Config SSM parameter | `/nzshm-backup/prod/config` |

---

## Related

- [Daily Health Report user guide](../user-guide/health-report.md)
- [Enabling Notifications runbook](enabling-notifications.md)
- [Inventory Bucket Recovery runbook](inventory-bucket-recovery.md)
- [Production Deployment Log](../PROD-DEPLOY-LOG.md)
