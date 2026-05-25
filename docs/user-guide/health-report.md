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
  inventory means the diff query is running against old data
  (*class 3 → yellow*).
- **Source-vs-backup divergence** — single Athena query (see
  [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md))
  returning two counts in one scan:
  - `source_minus_backup`: keys source has that backup doesn't —
    *class 1 → red*, the backup system has actually failed.
  - `backup_minus_source`: orphan keys backup retains after source-side
    deletions — *class 2 → informational*. See
    [purge-from-backup.md](../operations/purge-from-backup.md) for the
    out-of-band removal procedure.
- **Source-count delta vs yesterday** — *class 2 → informational only*
  after ADR-009 (previously red). Surfaces cleanups and growth events
  without alarming.
- **DynamoDB PITR** — per configured table, confirms point-in-time
  recovery is enabled and reports the latest restorable timestamp
  (*class 1 → red* if disabled).

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
  ✓ toshi       inventory_age=3.5h      restore=—
        ℹ backup has 12,431 orphans (source-side deletions retained per ADR-006)
  ✓ ths         inventory_age=3.5h      restore=—
  ✓ static      inventory_age=3.5h      restore=—
        ℹ source grew by 47 objects vs yesterday (+0.0%)
  ✓ weka        inventory_age=3.5h      restore=passed

Configuration:
  Canary (daily): weka
  Today's rotated source: —
  Freshness threshold: 30.0h
```

Per-source line:

- `✓` / `⚠` / `✗` — per-source status (green / yellow / red).
- `inventory_age=<N>h` — hours since the most recent S3 Inventory report
  was delivered for this source. `n/a` means no inventory data
  available.
- `restore=passed|failed|—` — restore-test outcome. `—` means this
  source wasn't tested today (not the canary and not in today's
  rotation).

Indented sub-lines below the per-source line follow the ADR-009 signal
taxonomy:

- `⚠ <text>` — class-1/class-3 issues affecting the row's status (a
  failed restore-test, a stale inventory, **backup-missing-source-keys**,
  PITR disabled).
- `ℹ <text>` — class-2 informational notes (source-count delta, backup
  orphan accumulation). Never change the row colour; safe to scan past
  unless investigating something specific.

Headline subject line:
`NSHM backup health 2026-05-21 — GREEN (3/4)` — scan-friendly so you
can tell at a glance whether to open the message.

---

## Green / Yellow / Red

Each source rolls up to a single status; the report's overall status is
the worst per-source status.

Status colour comes from the [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md)
class taxonomy:

| Status | Condition |
|---|---|
| **🟢 Green** | All class-1 signals nominal and inventory is fresh. Class-2 informational lines may still appear in the body. |
| **🟡 Yellow** | Inventory is older than 30h (class 3) but present, and no class-1 signal fires. |
| **🔴 Red** | Any class-1 signal: restore test failed; PITR disabled on a configured DynamoDB table; inventory completely missing; **backup is missing keys that source has**. |

### When you see red — investigate-by-signal table

| Red signal | Likely causes | Where to look |
|---|---|---|
| `restore=failed` | Cross-account credentials expired; backup bucket data corrupted; Athena scan-bytes quota hit | CloudWatch logs for the backup Lambda; `backup test restore --source <name>` locally |
| `inventory_age=n/a` | S3 Inventory disabled on source/backup bucket; control-plane bucket lost | `docs/operations/inventory-bucket-recovery.md` |
| `⚠ backup is missing N source keys` | Last `backup run` failed silently or skipped these keys; cross-account read role lost permission; manifest pipeline regression | Run `backup status`, then `backup run --source <name> --dry-run` to see what the next sync would do |
| PITR disabled | Someone disabled PITR in the source account; PITR-watcher Lambda failed to re-enable | `aws dynamodb describe-continuous-backups --table-name <t>`; SSM parameter for pitr-watcher |
| All sources red simultaneously | Notification path itself is fine but the backup account/network is broken | Check console; check `backup status` |

### When you see a class-2 informational line

| Info line | What it means | When to act |
|---|---|---|
| `ℹ source grew by N objects vs yesterday` | Normal day-over-day source-side change. | Investigate only if N is unexpectedly large for the source. |
| `ℹ source dropped by N objects vs yesterday` | Source-side cleanup or pipeline change. | Confirm with the source-data owner; if intentional, no action needed (backup retains the keys per ADR-006). |
| `ℹ backup has N orphans (source-side deletions retained per ADR-006)` | Steady-state — backup is keeping deleted source keys. | Decide whether to leave (default) or run [purge-from-backup.md](../operations/purge-from-backup.md). |

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
      restore_sample_size: 10
```

| Key | Default | What it controls |
|---|---|---|
| `canary_source` | `weka` | Source restore-tested *every* day. Pick the smallest one. |
| `rotation_by_weekday` | `{0: ths, 2: toshi, 4: static}` | Additional source per weekday (0=Mon … 6=Sun). |
| `freshness_threshold_hours` | `30.0` | Inventory age above this flags **yellow** (class 3). |
| `restore_sample_size` | `10` | Objects sampled per restore test. Larger = slower + more confidence. |

> The previous `delta_pct_threshold` / `delta_abs_threshold` knobs were
> removed under [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md)
> — the source-count delta is now informational only, not a red signal,
> so the thresholds no longer apply. Remove them from your YAML when
> upgrading; Pydantic will reject the unknown keys.

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
| Object-count delta query (class-2 info) | `src/nzshm_backup/athena_inventory.py` (`count_delta`) |
| Source-vs-backup divergence (class-1 + class-2 in one scan) | `src/nzshm_backup/athena_inventory.py` (`divergence_counts`) |
| Restore test reuse | `src/nzshm_backup/commands/test.py` (`restore_test_source`) |
| AWS infrastructure (SNS topic, IAM) | `serverless.yml` (`BackupReportsTopic`) |
| Lambda dispatch | `src/nzshm_backup/lambda_handler.py`, `src/nzshm_backup/lambda_schema.py` |
| EventBridge schedule CLI | `src/nzshm_backup/commands/schedule.py` (`--task-type` flag) |

---

## Related

- [ADR-005](../design/adr/ADR-005-weekly-health-report.md) — design
  rationale (cadence, channels, SES rejection)
- [ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
  mit. 1 — original object-count delta intent (now reclassified by ADR-009)
- [ADR-007](../design/adr/ADR-007-harden-inventory-control-plane-bucket.md)
  mit. 4 — freshness watchdog
- [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md) —
  signal-class taxonomy (class 1/2/3) currently in force
- [purge-from-backup.md](../operations/purge-from-backup.md) — out-of-band
  procedure for removing the class-2 orphans this report surfaces
- [enabling-notifications.md](../operations/enabling-notifications.md)
  — channel turn-on runbook
- [inventory-bucket-recovery.md](../operations/inventory-bucket-recovery.md)
  — recovery for the control-plane bucket the report depends on
