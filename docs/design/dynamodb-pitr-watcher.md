# DynamoDB PITR Watcher: Automatic PITR Re-enable After Restore

## Problem

AWS does **not** automatically re-enable Point-in-Time Recovery (PITR) on a
DynamoDB table restored via `RestoreTableToPointInTime`. The restored table is
created with PITR disabled. If left unaddressed, the restored table has no
ongoing point-in-time protection — a dangerous state in a DR scenario where
the operator is already under pressure.

---

## Design

An event-driven Lambda (`pitr-watcher`) polls pending restores and re-enables
PITR as soon as each restored table reaches `ACTIVE` status.

### Components

```
backup restore run
  │
  ├── submit RestoreTableToPointInTime
  ├── append to SSM /nzshm-backup/pending-restores
  └── events:EnableRule → nzshm-backup-pitr-watcher (rate: 5 min)

        every 5 min ↓

pitr-watcher Lambda
  ├── read SSM /nzshm-backup/pending-restores
  ├── for each pending entry:
  │     dynamodb:DescribeTable → if ACTIVE + PITR disabled:
  │       dynamodb:UpdateContinuousBackups (enable PITR)  ✓
  │       mark pitr_enabled = true in state
  ├── write updated state back to SSM
  └── if pending list is empty:
        events:DisableRule → nzshm-backup-pitr-watcher
        (rule stays deployed, silent until next restore)
```

### State: SSM Parameter Store

**Parameter name:** `/nzshm-backup/pending-restores`
**Type:** `String` (JSON)

```json
{
  "pending": [
    {
      "source": "arkivalist",
      "target_table": "arkivalist-api-dev-events-restored",
      "submitted_at": "2026-03-17T23:00:00Z",
      "restore_point": "2026-03-17T22:59:00Z",
      "pitr_enabled": false
    }
  ]
}
```

SSM is used (rather than S3) because this is process state, not backup data.
It has no source-account dependency and is a single well-known location for
the watcher regardless of how many sources are configured.

---

## Concurrent restore limit

### SSM constraint

SSM Standard parameters have a **4 096-byte limit**. Each pending-restore
entry is approximately 173 bytes; at 15 concurrent restores the payload is
~2 600 bytes, leaving comfortable headroom for wrapper overhead and future
field additions. Beyond 15 entries the parameter approaches the limit and
a write may be rejected by SSM.

**Hard limit enforced by `restore run`:** **15 concurrent pending restores**

### Guard in `restore run`

Before submitting a DynamoDB restore and writing to SSM, `restore run` must:

1. Read `/nzshm-backup/pending-restores` (or treat a missing parameter as empty)
2. Count entries where `pitr_enabled = false`
3. Check that `current_pending + tables_being_restored <= 15`
4. If exceeded, abort with a clear error:

```
Error: too many concurrent pending restores (current: 15, limit: 15).
Wait for existing restores to complete (PITR will be re-enabled automatically),
then retry. Check progress with: backup restore status --source <source>
```

This prevents a silent SSM write failure mid-restore, which would leave a
table permanently unprotected.

---

## EventBridge rule lifecycle

The rule `nzshm-backup-pitr-watcher` is **deployed disabled** as part of the
standing infrastructure. It is never created or deleted at runtime — only
enabled and disabled:

| Action | Who | When |
|--------|-----|------|
| Deploy (disabled) | IaC / `serverless.yml` | Once, at deploy time |
| Enable | `restore run` CLI | After submitting ≥1 DynamoDB restore |
| Disable | `pitr-watcher` Lambda | When pending list drains to empty |

Using a pre-deployed rule avoids needing `events:PutRule` /
`events:DeleteRule` in the CLI IAM policy, and ensures the rule ARN is
stable and known at deploy time.

---

## IAM requirements

### `restore run` CLI role / operator

- `ssm:GetParameter` — read current pending list
- `ssm:PutParameter` — append new entry
- `events:EnableRule` — activate the watcher rule

### `pitr-watcher` Lambda execution role

- `ssm:GetParameter` + `ssm:PutParameter` on `/nzshm-backup/pending-restores`
- `dynamodb:DescribeTable` — check restore status
- `dynamodb:UpdateContinuousBackups` — re-enable PITR
- `events:DisableRule` on `nzshm-backup-pitr-watcher` (self)

---

## `--no-pitr` override

`restore run` accepts `--no-pitr` to skip writing to SSM and enabling the
watcher. Use this only for short-lived test restores that will be deleted
immediately. Default is PITR-on.

---

## Current status

**Not yet implemented.** Tracked as next DynamoDB restore milestone.

Pending work:
- [ ] `pitr-watcher` Lambda (`src/nzshm_backup/lambda_pitr_watcher.py`)
- [ ] EventBridge rule + IAM role in `serverless.yml`
- [ ] SSM state helpers (`src/nzshm_backup/restore_state.py`)
- [ ] `restore run` guard (read SSM, enforce 15-entry limit, write on submit)
- [ ] `restore run` `--no-pitr` flag
- [ ] `restore status` output: show `pitr_enabled` column for pending restores

**Created:** 2026-03-18
