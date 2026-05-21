# Daily Health Report

A scheduled Lambda task that builds a per-source picture of the backup
system and delivers it to Slack and email. Complementary to the
CloudWatch Lambda-errors alarm (ADR-005 fast path) — the alarm catches
*hard* Lambda failures within minutes; the daily report catches *silent*
issues that don't throw exceptions.

| | Fast path (alarm) | Slow path (this report) |
|---|---|---|
| Source | CloudWatch alarm on Lambda `Errors` | Daily Lambda task |
| Cadence | Within minutes of failure | Once per day (14:30 NZST) |
| Catches | Hard Lambda invocation errors | Stale inventory, count drops, restore failures, PITR disabled |

---

## What gets checked

Per source, every day:

- **Backup state** — last run status (skipped / submitted / failed),
  recent S3 Batch jobs, DynamoDB exports.
- **Inventory freshness** — most recent S3 Inventory report's age. Stale
  inventory means the diff query is running against old data.
- **Object-count delta** — source-bucket object count today vs yesterday
  (Athena `COUNT(*)` against the two latest inventory partitions). A
  large drop is the only loud signal of an intentional source deletion;
  see ADR-006 mitigation 1.
- **DynamoDB PITR** — per configured table, confirms point-in-time
  recovery is enabled and reports the latest restorable timestamp.

For one or two sources each day:

- **Sampled restore verification** — pulls 10 random objects from the
  backup bucket via Athena, copies them to a temp bucket, verifies
  checksums or ETags, deletes the temp bucket. Proves the restore path
  is functional end-to-end.

---

## Canary + rotation

The restore test is the expensive operation — sampling, copying, and
verifying objects. Running it for every source every day would be wasteful
on the large sources (toshi 8 TB, ths 1 TB, static 2.7 TB). Instead:

- **Canary** (`canary_source` in config) — restore-tested **every day**.
  Default: `weka` (3 objects, ~15s, cost-trivial). Exercises the full
  IAM + Athena + S3 + checksum path daily so credential/permission/
  pipeline failures are caught within 24 hours regardless of which
  larger source's turn it is.
- **Weekday rotation** (`rotation_by_weekday`) — adds one large source
  to today's restore set based on the weekday.

Default rotation:

| Day | Restore-tested |
|---|---|
| Monday | weka + **ths** |
| Tuesday | weka |
| Wednesday | weka + **toshi** |
| Thursday | weka |
| Friday | weka + **static** |
| Saturday | weka |
| Sunday | weka |

So every configured source gets a verified restore at least once per
week.

---

## Reading a report

Sample text email body (also delivered as a Slack Block Kit message):

```
NSHM Backup Health Report — 2026-05-21

Overall: GREEN  (4/4 sources healthy)
Build time: 132.3s

Per source:
  ✓ toshi       inventory_age=3.5h      delta=+0 (+0.0%)            restore=—
  ✓ ths         inventory_age=3.5h      delta=+0 (+0.0%)            restore=—
  ✓ static      inventory_age=3.5h      delta=+0 (+0.0%)            restore=—
  ✓ weka        inventory_age=3.5h      delta=+0 (+0.0%)            restore=passed

Configuration:
  Canary (daily): weka
  Today's rotated source: —
  Freshness threshold: 30.0h
  Delta thresholds: -10,000 absolute or -5.0% (whichever crossed first)
```

Per-source line:

- `✓` / `⚠` / `✗` — per-source status (green / yellow / red).
- `inventory_age=<N>h` — hours since the most recent S3 Inventory report
  was delivered for this source. `n/a` means no inventory data
  available.
- `delta=<N> (<P>%)` — today's source object count minus yesterday's
  (raw + percent). `+0` is the steady-state for immutable scientific
  data.
- `restore=passed|failed|—` — restore-test outcome. `—` means this
  source wasn't tested today (not the canary and not in today's
  rotation).

Headline subject line:
`NSHM backup health 2026-05-21 — GREEN (3/4)` — scan-friendly so you
can tell at a glance whether to open the message.

---

## Green / Yellow / Red

Each source rolls up to a single status; the report's overall status is
the worst per-source status.

| Status | Condition |
|---|---|
| **🟢 Green** | All signals normal. |
| **🟡 Yellow** | Inventory is older than 30h (default) but present. |
| **🔴 Red** | Any of: restore test failed; PITR disabled on a configured DynamoDB table; inventory completely missing; object-count drop ≥ 5% or ≥ 10,000 objects. |

### When you see red — investigate-by-signal table

| Red signal | Likely causes | Where to look |
|---|---|---|
| `restore=failed` | Cross-account credentials expired; backup bucket data corrupted; Athena scan-bytes quota hit | CloudWatch logs for the backup Lambda; `backup test restore --source <name>` locally |
| `inventory_age=n/a` | S3 Inventory disabled on source/backup bucket; control-plane bucket lost | `docs/operations/inventory-bucket-recovery.md` |
| Large `delta=-N` drop | Intentional source deletion; bug in source pipeline silently deleted data | Source bucket history; confirm with source-data owner before touching backup |
| PITR disabled | Someone disabled PITR in the source account; PITR-watcher Lambda failed to re-enable | `aws dynamodb describe-continuous-backups --table-name <t>`; SSM parameter for pitr-watcher |
| All sources red simultaneously | Notification path itself is fine but the backup account/network is broken | Check console; check `backup status` |

When in doubt: run `backup health-report run` manually with prod
credentials to reproduce. The CLI prints the same content the email
delivered.

---

## Cadence

EventBridge fires the Lambda once a day at **14:30 NZST** (= 02:30 UTC).
That's 1h25m after the daily backup schedules fire at 13:05 NZST,
giving the largest S3 Batch jobs time to complete so the report sees
post-run state.

The cron expression and rule live as AWS resources, not in
`backup-config.yaml`. Manage them via the `backup schedule` CLI:

```bash
# Create / replace the daily schedule
backup schedule add --source _health --task-type health_report \
    --frequency daily --time 14:30-NZST

# Inspect
backup schedule show

# Pause without deleting
backup schedule disable --source _health --task-type health_report --frequency daily

# Re-enable
backup schedule enable --source _health --task-type health_report --frequency daily

# Remove
backup schedule remove --source _health --task-type health_report --frequency daily
```

`--source _health` is a sentinel — the schema requires `source` but the
health-report dispatch path ignores it. The rule name is fixed at
`nzshm-backup-health-report-daily` regardless of the value passed.

Manual invocation (e.g. for incident response or post-deploy smoke
test) is always available too — see [Manual invocation](#manual-invocation)
below.

---

## Tuning

Five (six) knobs live under `notifications.reports.health` in
`backup-config.production.yaml`:

```yaml
notifications:
  reports:
    health:
      canary_source: weka
      rotation_by_weekday:        # ISO weekday → source restore-tested
        0: ths                    # Monday
        2: toshi                  # Wednesday
        4: static                 # Friday
      freshness_threshold_hours: 30.0
      delta_pct_threshold: -5.0   # source-count drop ≥ 5% → red
      delta_abs_threshold: -10000 # OR drop ≥ 10k objects → red (first to cross wins)
      restore_sample_size: 10
```

| Key | Default | What it controls |
|---|---|---|
| `canary_source` | `weka` | Source restore-tested *every* day. Pick the smallest one. |
| `rotation_by_weekday` | `{0: ths, 2: toshi, 4: static}` | Additional source per weekday (0=Mon … 6=Sun). |
| `freshness_threshold_hours` | `30.0` | Inventory age above this flags **yellow**. |
| `delta_pct_threshold` | `-5.0` | Source-count drop ≥ this % flags **red**. Must be negative. |
| `delta_abs_threshold` | `-10000` | OR drop ≥ this absolute count flags red. First to cross wins. |
| `restore_sample_size` | `10` | Objects sampled per restore test. Larger = slower + more confidence. |

Defaults are baked into `src/nzshm_backup/health_report.py`; the YAML
overrides are read with `getattr` fallback so omitting the block is
safe.

To **disable** a rotation day, remove the map entry (only the canary
runs that day). To **add** a Saturday test, add `5: <source-alias>`.
The source alias must exist under the top-level `sources:` block or the
entry is silently ignored.

---

## Delivery channels

Two independent channels — turn either or both on. Setup procedure for
each is in `docs/operations/enabling-notifications.md`.

| Channel | Config key | Setup prerequisites |
|---|---|---|
| Slack | `notifications.slack.enabled: true` | Incoming webhook URL stored as `backup-slack-webhook` in Secrets Manager |
| SNS email | `notifications.reports.email.enabled: true` + `address: ...` | Subscription confirmed by clicking AWS confirmation email link |

Channels are independent: if Slack fails, SNS still tries (and vice
versa). The CLI prints per-channel status in the Delivery summary:

```
Delivery:
  Slack: ok
  SNS:   ok (MessageId=...)
```

To temporarily snooze one channel: set its `enabled: false` and push the
config (no deploy needed for Slack; SNS subscription is managed by
CloudFormation so removing the address requires a deploy).

---

## Manual invocation

```bash
# Print the report locally, no delivery. Skips the slow restore tests.
uv run backup health-report preview

# Full report (incl. restore tests), print only.
uv run backup health-report run

# Full report + deliver via enabled channels.
uv run backup health-report run --send

# Force rotation as if today were Monday (testing rotation logic).
uv run backup health-report run --weekday 0

# Override the SNS topic (rare; auto-resolved from session+stage by default).
uv run backup health-report run --send --topic-arn arn:aws:sns:...
```

`--send` requires either local credentials with `sns:Publish` +
`secretsmanager:GetSecretValue` on the relevant resources, or the
Lambda's IAM role at runtime. The deployed Lambda has both.

---

## Where the code lives

| Concern | File |
|---|---|
| Orchestration, classification, formatting | `src/nzshm_backup/health_report.py` |
| CLI commands | `src/nzshm_backup/commands/health_report.py` |
| Tunable defaults / config schema | `src/nzshm_backup/config/models.py` (`HealthReportConfig`) |
| Slack delivery | `src/nzshm_backup/notifications/slack.py` |
| SNS delivery | `src/nzshm_backup/notifications/sns.py` |
| Inventory freshness reuse | `src/nzshm_backup/inventory_state.py` |
| Object-count delta query | `src/nzshm_backup/athena_inventory.py` (`count_delta`) |
| Restore test reuse | `src/nzshm_backup/commands/test.py` (`restore_test_source`) |
| AWS infrastructure (SNS topic, IAM) | `serverless.yml` (`BackupReportsTopic`) |
| Lambda dispatch | `src/nzshm_backup/lambda_handler.py`, `src/nzshm_backup/lambda_schema.py` |
| EventBridge schedule CLI | `src/nzshm_backup/commands/schedule.py` (`--task-type` flag) |

---

## Related

- ADR-005 — design rationale (cadence, channels, SES rejection)
- ADR-006 mitigation 1 — object-count delta check
- ADR-007 mitigation 4 — freshness watchdog
- `docs/operations/enabling-notifications.md` — channel turn-on runbook
- `docs/operations/inventory-bucket-recovery.md` — recovery for the
  control-plane bucket the report depends on
