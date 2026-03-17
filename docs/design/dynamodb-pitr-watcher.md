# DynamoDB PITR Watcher: Automatic PITR Re-enable After Restore

## Problem

AWS does **not** automatically re-enable Point-in-Time Recovery (PITR) on a
DynamoDB table restored via `RestoreTableToPointInTime`. The restored table is
created with PITR disabled. If left unaddressed, the restored table has no
ongoing point-in-time protection — a dangerous state in a DR scenario where
the operator is already under pressure.

---

## Design

An event-driven Lambda (`pitr-watcher`) polls for restored tables and re-enables
PITR as soon as each reaches `ACTIVE` status.

### State mechanism: DynamoDB tags (not SSM)

The tag `PITRPending=true` is set on the restored table **at submission time**
via the `Tags` parameter of `RestoreTableToPointInTime`. The tag is the source
of truth — no external state store is needed.

This avoids the read-modify-write race condition that would affect an SSM-based
design (SSM has no compare-and-swap operation). The tag is set atomically as
part of the restore API call itself.

### Components

```
backup restore run
  │
  ├── RestoreTableToPointInTime(Tags=[PITRPending=true, RestoredBy=nzshm-backup, ...])
  └── events:EnableRule → nzshm-backup-pitr-watcher (rate: 5 min)

        every 5 min ↓

pitr-watcher Lambda
  ├── tag:GetResources(TagFilters=[PITRPending=true], ResourceTypeFilters=[dynamodb:table])
  ├── for each table found:
  │     dynamodb:DescribeTable → if ACTIVE:
  │       dynamodb:UpdateContinuousBackups (enable PITR)  ✓
  │       dynamodb:UntagResource → remove PITRPending tag
  └── if no PITRPending=true tables remain:
        events:DisableRule → nzshm-backup-pitr-watcher
        (rule stays deployed, silent until next restore)
```

### Tags set at restore submission

```python
Tags=[
    {"Key": "RestoredBy",   "Value": "nzshm-backup"},
    {"Key": "PITRPending",  "Value": "true"},
    {"Key": "RestoredFrom", "Value": source_table_name},
    {"Key": "RestoredAt",   "Value": restore_point.isoformat()},
]
```

`RestoredBy` and `RestoredAt` are informational and persist after PITR is
re-enabled. `PITRPending` is removed once PITR is enabled — its absence
is the completion signal.

---

## Why tags, not SSM

An earlier design used SSM Parameter Store (`/nzshm-backup/pending-restores`)
for pending restore state. This was replaced because:

| Concern | SSM design | Tag design |
|---------|-----------|------------|
| Race condition (CLI write vs Lambda write) | Yes — no CAS on SSM | No — tag set atomically at API call |
| Concurrent restore limit | 15 entries (4 096-byte parameter limit) | No limit |
| External state to manage | Yes — SSM parameter lifecycle | No — tag lives on the table |
| Discovery if state is lost | Silent failure | Tag scan always finds unprotected tables |
| Self-documenting | No | Yes — tag visible in console/CLI |

The tag-based design is stateless from the watcher's perspective: it scans
for `PITRPending=true` tables, acts on them, and removes the tag. No external
state can get out of sync.

---

## EventBridge rule lifecycle

The rule `nzshm-backup-pitr-watcher` is **deployed disabled** as part of the
standing infrastructure. It is never created or deleted at runtime — only
enabled and disabled:

| Action | Who | When |
|--------|-----|------|
| Deploy (disabled) | IaC / `serverless.yml` | Once, at deploy time |
| Enable | `restore run` CLI | After submitting ≥1 DynamoDB restore |
| Disable | `pitr-watcher` Lambda | When no `PITRPending=true` tables remain |

Using a pre-deployed rule avoids needing `events:PutRule` /
`events:DeleteRule` in the CLI IAM policy, and ensures the rule ARN is
stable and known at deploy time.

---

## IAM requirements

### `restore run` CLI role / operator

- `events:EnableRule` on `nzshm-backup-pitr-watcher`
- No new DynamoDB permissions needed — tags are passed into `RestoreTableToPointInTime`

### `pitr-watcher` Lambda execution role

- `tag:GetResources` — scan for `PITRPending=true` tables
- `dynamodb:DescribeTable` — check table status
- `dynamodb:UpdateContinuousBackups` — re-enable PITR
- `dynamodb:UntagResource` — remove `PITRPending` tag on completion
- `events:DisableRule` on `nzshm-backup-pitr-watcher` (self)

---

## `--no-pitr` override

`restore run` accepts `--no-pitr` to omit the `PITRPending` tag and skip
enabling the watcher rule. Use this only for short-lived test restores that
will be deleted immediately. Default is PITR-on.

---

## Current status

**Not yet implemented.** Tracked as next DynamoDB restore milestone.

Pending work:
- [ ] `pitr-watcher` Lambda (`src/nzshm_backup/lambda_pitr_watcher.py`)
- [ ] EventBridge rule + IAM role in `serverless.yml`
- [ ] `restore run`: pass `PITRPending=true` tag in `RestoreTableToPointInTime`
- [ ] `restore run`: `--no-pitr` flag
- [ ] `restore run`: `events:EnableRule` call after successful restore submission
- [ ] `restore status`: show `PITRPending` tag state alongside table restore status

**Created:** 2026-03-18
