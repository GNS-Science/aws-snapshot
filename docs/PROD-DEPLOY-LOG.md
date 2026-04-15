# Production Deployment Log

**Backup account:** `737696831915`
**Source account:** `461564345538`
**Region:** `ap-southeast-2`
**Date started:** 2026-04-15

---

## Step 1 — Inspect source account and create config (2026-04-15)

Authenticated to source account `461564345538` via SSO (`nshm-admin` profile) and listed
DynamoDB tables and S3 buckets to identify production data candidates.

### DynamoDB tables found (with `-PROD` suffix)

```
SGI-BinaryLargeObject-PROD
THS_DisaggAggregationExceedance-PROD
THS_GriddedHazard-PROD
ToshiFileObject-PROD
ToshiIdentity-PROD
ToshiOpenquakeHazardCurveRlzs-PROD
ToshiOpenquakeHazardCurveRlzsV2-PROD
ToshiOpenquakeHazardCurveStats-PROD
ToshiOpenquakeHazardCurveStatsV2-PROD
ToshiOpenquakeHazardMeta-PROD
ToshiTableObject-PROD
ToshiThingObject-PROD
```

### S3 buckets found (production data candidates)

```
nzshm22-toshi-api-prod
ths-dataset-prod
nzshm22-static-reports
```

### Selected for backup

| Source alias | Type     | Resource                              |
|-------------|----------|---------------------------------------|
| `toshi`     | S3       | `nzshm22-toshi-api-prod`              |
| `toshi`     | DynamoDB | `ToshiFileObject-PROD`                |
| `toshi`     | DynamoDB | `ToshiIdentity-PROD`                  |
| `toshi`     | DynamoDB | `ToshiTableObject-PROD`               |
| `toshi`     | DynamoDB | `ToshiThingObject-PROD`               |
| `ths`       | S3       | `ths-dataset-prod`                    |
| `ths`       | S3       | `nzshm22-static-reports`              |

Excluded: hazard curve tables, SGI table, serverless deployment buckets, existing backup buckets
(`nzshm22-toshi-api-prod-backup`, `ths-table-backup`).

### Config file created

`backup-config.production.yaml` — cross-account config with:
- `toshi`: S3 Batch enabled (`use_s3_batch: true`) for large bucket
- `ths`: direct incremental copy (`use_s3_batch: false`)
- Both sources: `source_account_role_arn` and `source_account_restore_role_arn` set to `null`
  pending IAM role creation (steps 2–3 below)

---

## Step 2 — Create S3 Batch role in backup account ✅ 2026-04-15

**Run as:** backup account `737696831915`

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
python scripts/create-backup-roles.py --config backup-config.production.yaml
```

Created role: `arn:aws:iam::737696831915:role/nzshm-backup-batch-role`
Inline policy `nzshm-backup-batch-permissions` attached.
`general.s3_batch_role_arn` written back to `backup-config.production.yaml`.

---

## Step 3 — Create source IAM roles in source account ✅ 2026-04-15

**Run as:** source account `461564345538` (`nshm-admin` profile)

Initial attempt ran per-source, which caused the second run (`ths`) to overwrite the reader
role policy set by the first (`toshi`), losing toshi bucket permissions. Corrected by running
once in explicit mode covering all buckets and tables across both sources.

```bash
uv run python scripts/create-source-roles.py \
    --backup-account-id 737696831915 \
    --s3-buckets nzshm22-toshi-api-prod ths-dataset-prod nzshm22-static-reports \
    --dynamodb-tables ToshiFileObject-PROD ToshiIdentity-PROD ToshiTableObject-PROD ToshiThingObject-PROD \
    --batch-role-arn arn:aws:iam::737696831915:role/nzshm-backup-batch-role \
    --profile nshm-admin
```

Note: `--backup-account-id` is required because `general.lambda_arn` is not yet set (Lambda
not yet deployed). Script was patched to accept this flag alongside `--config/--source`.

Roles created/updated in `461564345538`:
- `arn:aws:iam::461564345538:role/nzshm-backup-reader` — read all 3 source buckets + 4 DynamoDB tables
- `arn:aws:iam::461564345538:role/nzshm-backup-restore` — PITR restore + tag management

S3 bucket policies applied to all 3 source buckets allowing `nzshm-backup-batch-role`
read (backup direction) and write (restore direction).
Restore target buckets (`*-restore`) skipped — don't exist yet, policy applied at restore time.

**Known limitation:** running `create-source-roles.py` per-source for the same account
overwrites the shared role's inline policy. Always use explicit mode (all buckets/tables in
one invocation) when multiple sources share an account, or re-run for the last source last.

---

## Step 4 — Deploy Lambda to backup account ✅ 2026-04-15

**Run as:** backup account `737696831915`

```bash
export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh" && nvm use v22
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
serverless deploy --stage prod
```

Stack deployed: `nzshm-backup-service-prod`

| Function | ARN |
|----------|-----|
| `backup` | `arn:aws:lambda:ap-southeast-2:737696831915:function:nzshm-backup-service-prod-backup` |
| `pitr-watcher` | `arn:aws:lambda:ap-southeast-2:737696831915:function:nzshm-backup-service-prod-pitr-watcher` |

`general.lambda_arn` updated in `backup-config.production.yaml`.

Notes:
- Serverless Framework org: `gnssciencenshm` (GitHub-linked GNS account, user `chrisbc`)
- `accountId` is not a valid Serverless Framework v4 provider field — removed; account safety
  is enforced via the `nshm-backup-admin` SSO profile instead.

---

## Step 5 — Push config to SSM and run dry-run ✅ 2026-04-15

**Run as:** backup account `737696831915`

`BACKUP_CONFIG_PATH` must be set — the `backup config` subcommands had a bug where they
ignored this env var (fixed in this session, `commands/config.py`). `--stage prod` is also
required; the default stage is `dev`.

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod
```

Config pushed to SSM parameter: `/nzshm-backup/prod/config`

Dry runs (still running at time of writing — large buckets):

```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source toshi --dry-run
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source ths --dry-run
```

Note: dry-run for `toshi` does a full local object listing (~8M objects, ~80k S3 API pages)
even though the real run uses S3 Batch. Expect this to take 10–20 minutes. The actual backup
run submits a Batch job and returns immediately.

Dry-run approach abandoned — for S3 Batch sources, listing 8M objects locally is slow and
unrepresentative. Fixed: `batch_backup_source` dry-run now does a single access-check call
instead. Added `backup check` pre-flight command (see Step 5b).

### Step 5b — Pre-flight check ✅ 2026-04-15

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup check
```

Results:
- All IAM role assumptions: PASS
- All source bucket reads: PASS
- Backup buckets: WARN (don't exist yet — created on first run, expected)
- S3 Batch role: PASS
- DynamoDB PITR: WARN (all 4 Toshi tables disabled — fixed in Step 5c)

### Step 5c — Enable PITR on Toshi DynamoDB tables ✅ 2026-04-15

**Run as:** source account `461564345538`

```bash
eval $(aws configure export-credentials --profile nshm-admin --format env)
uv run python scripts/enable-pitr.py \
    --tables ToshiFileObject-PROD ToshiIdentity-PROD ToshiTableObject-PROD ToshiThingObject-PROD
```

All 4 tables: PITR ENABLED. Allow a few minutes before running first DynamoDB export.

---

## Step 6 — Add weka source and smoke-test ✅ 2026-04-15

Added `nzshm22-weka-ui-prod` as a `weka` source (80MB, ~64 objects) in
`backup-config.production.yaml`. Used as a minimal smoke test before running the large sources.

S3 bucket policy update required in source account (bucket wasn't covered by initial
`create-source-roles.py` run):

```bash
eval $(aws configure export-credentials --profile nshm-admin --format env)
uv run python scripts/create-source-roles.py \
    --backup-account-id 737696831915 \
    --s3-buckets nzshm22-toshi-api-prod ths-dataset-prod nzshm22-static-reports nzshm22-weka-ui-prod \
    --dynamodb-tables ToshiFileObject-PROD ToshiIdentity-PROD ToshiTableObject-PROD ToshiThingObject-PROD \
    --batch-role-arn arn:aws:iam::737696831915:role/nzshm-backup-batch-role \
    --profile nshm-admin
```

Pre-flight check:

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup check --source weka
```

All checks passed (backup bucket WARN — doesn't exist yet, expected).

First live backup run (smoke test):

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env) && \
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source weka
```

Result: **SUCCESS** — 64 objects, ~80MB copied in ~7 seconds. Backup bucket created automatically.

Note: `nzshm22-static-reports` was initially included in the `ths` source but turned out to be
~40M objects / 2.7TB — not a small bucket. Separated into its own `static` source in the config.
That source was killed mid-dry-run; will be scheduled separately.

---

## Step 7 — Schedule weka as EventBridge smoke test ✅ 2026-04-15

Before scheduling the large sources, added a daily schedule for `weka` to verify the
EventBridge → Lambda trigger path end-to-end. Scheduled to fire at 14:21 NZST (02:21 UTC)
— approximately 5 minutes after the rule was created.

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add \
    --source weka \
    --frequency daily \
    --time "14:21 NZST"
```

Output:
```
Rule 'nzshm-backup-weka-daily' created/updated: cron(21 2 * * ? *)  → 14:21 NZST locally
Target registered: arn:aws:lambda:ap-southeast-2:737696831915:function:nzshm-backup-service-prod-backup
```

Two disabled stub rules (`nzshm-backup-service-prod-backup-rule-1/2`) are leftover from
Serverless Framework deployment — not a concern.

```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule show
```

```
Rule Name                                     State      Schedule                       Local time
----------------------------------------------------------------------------------------------------
nzshm-backup-pitr-watcher                     DISABLED   rate(5 minutes)
nzshm-backup-service-prod-backup-rule-1       DISABLED   rate(7 days)
nzshm-backup-service-prod-backup-rule-2       DISABLED   rate(1 day)
nzshm-backup-weka-daily                       ENABLED    cron(21 2 * * ? *)             → 14:21 NZST locally
```

**14:21 NZST trigger — FAILED.** Lambda fired correctly but errored: `Unknown source alias: weka`.
Root cause: config in SSM (`/nzshm-backup/prod/config`) was stale — `weka` source had not been
pushed after being added to `backup-config.production.yaml`.

Fix — push updated config to SSM:

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod
```

Rule updated to fire 3 minutes later (14:34 NZST) as a re-test:

```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add \
    --source weka \
    --frequency daily \
    --time "14:34 NZST"
```

**14:34 NZST trigger — SUCCESS.** 1 object copied (incremental — only the event log was new
since the earlier manual run). EventBridge → Lambda → SSM config → S3 path confirmed working.

**Lesson:** always push config to SSM after modifying `backup-config.production.yaml`:
```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod
```

---

## Step 8 — Schedule and run large sources

Once weka scheduled run confirms the trigger path works:

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source toshi --frequency daily --time "02:00 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source ths --frequency daily --time "02:00 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source static --frequency daily --time "02:00 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source toshi
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source ths
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup status
```

_Status: pending weka smoke-test result_
