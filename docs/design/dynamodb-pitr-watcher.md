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
PITR as soon as each table reaches `ACTIVE` status.

### State mechanism: SSM Parameter Store

At restore submission time, `restore run` writes an entry to SSM
(`/nzshm-backup/pending-restores`) containing the restore ARN, source alias,
source table ARN, restore point, and submission timestamp. The EventBridge rule
is enabled immediately — the watcher can start polling without waiting for the
table to be visible.

The watcher reads SSM, calls `describe_table` per entry, and when a table
reaches `ACTIVE`: enables PITR, applies informational tags, removes the entry
from SSM. When the list is empty it disables the rule.

### Why SSM, not tags

An earlier design used a `PITRPending=true` tag set at submission time. This
failed because `RestoreTableToPointInTime` does not accept a `Tags` parameter,
and the subsequent `tag_resource` call fails with `ResourceNotFoundException`
for at least 60 seconds after submission (the table is not yet visible to the
resource API). No reliable retry window exists at submission time.

SSM is written to in the backup account immediately, before the table registers
anywhere. The watcher discovers pending restores from SSM rather than tag scan,
so discovery is never blocked by table visibility delay.

### Components

```
backup restore run
  │
  ├── RestoreTableToPointInTime(SourceTableArn, TargetTableName, RestoreDateTime)
  ├── ssm:PutParameter → /nzshm-backup/pending-restores (append entry)
  └── events:EnableRule → nzshm-backup-pitr-watcher (rate: 5 min)

        every 5 min ↓

pitr-watcher Lambda
  ├── ssm:GetParameter → /nzshm-backup/pending-restores
  ├── for each entry (grouped by source):
  │     assume source_account_restore_role_arn (cross-account)
  │     dynamodb:DescribeTable → if ACTIVE:
  │       dynamodb:UpdateContinuousBackups (enable PITR)       ✓
  │       dynamodb:TagResource → RestoredBy, RestoredFrom, RestoredAt
  │       remove entry from SSM list
  │     else: keep entry, retry next invocation
  ├── ssm:PutParameter → write back remaining entries
  └── if no entries remain:
        events:DisableRule → nzshm-backup-pitr-watcher
        (rule stays deployed, silent until next restore)
```

### Tags applied by the watcher (once ACTIVE)

```python
Tags=[
    {"Key": "RestoredBy",   "Value": "nzshm-backup"},
    {"Key": "RestoredFrom", "Value": source_table_name},
    {"Key": "RestoredAt",   "Value": restore_point.isoformat()},
]
```

These are informational only — they persist on the table after restore and are
visible in the console. Unlike the old `PITRPending` tag, they are not used for
discovery.

---

## EventBridge rule lifecycle

The rule `nzshm-backup-pitr-watcher` is **deployed disabled** as part of the
standing infrastructure. It is never created or deleted at runtime — only
enabled and disabled:

| Action | Who | When |
|--------|-----|------|
| Deploy (disabled) | IaC / `serverless.yml` | Once, at deploy time |
| Enable | `restore run` CLI | After submitting ≥1 DynamoDB restore (unless `--no-pitr`) |
| Disable | `pitr-watcher` Lambda | When SSM pending list is empty |

Using a pre-deployed rule avoids needing `events:PutRule` /
`events:DeleteRule` in the CLI IAM policy, and ensures the rule ARN is
stable and known at deploy time.

---

## IAM requirements

### `restore run` CLI / operator

- `events:EnableRule` on `nzshm-backup-pitr-watcher`
- `ssm:GetParameter` + `ssm:PutParameter` on `/nzshm-backup/*`

### `pitr-watcher` Lambda execution role (backup account)

- `ssm:GetParameter` + `ssm:PutParameter` on `/nzshm-backup/*`
- `sts:AssumeRole` — to assume `source_account_restore_role_arn` cross-account
- `events:DisableRule` on `nzshm-backup-pitr-watcher`

### `nzshm-backup-restore` role (source account, assumed cross-account)

- `dynamodb:DescribeTable` on restored tables
- `dynamodb:UpdateContinuousBackups` on restored tables
- `dynamodb:TagResource` on restored tables
- (Currently granted via `dynamodb:*` on `table/*` — see `scripts/create-source-roles.py`)

---

## `--no-pitr` override

`restore run` accepts `--no-pitr` to skip writing to SSM and skip enabling the
watcher rule. Use this only for short-lived test restores that will be deleted
immediately. Default is PITR-on.

---

## Concurrent restore limit

SSM String parameters have a maximum value size of **4 096 bytes**. Each
pending restore entry is approximately 200–250 bytes of JSON. This gives a
practical limit of roughly **15–20 concurrent pending restores** per SSM
parameter.

This limit is unlikely to be reached in normal operation (DynamoDB PITR
restores are rare, deliberate events). If it is ever a concern, the SSM
parameter can be replaced with an SSM StringList or a DynamoDB table.

---

## Current status

**Implemented and tested** (2026-03-18).

- [x] `pitr-watcher` Lambda (`src/nzshm_backup/lambda_pitr_watcher.py`)
- [x] `restore_state.py` — SSM read/write helpers
- [x] EventBridge rule + IAM in `serverless.yml`
- [x] `restore run`: writes to SSM, enables rule, `--no-pitr` flag
- [x] Cross-account restore role (`scripts/create-source-roles.py`)
- [x] Verified end-to-end against arkivalist (cross-account, ap-southeast-2)
