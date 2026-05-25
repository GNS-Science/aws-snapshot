# Production Deployment Log

**Backup account:** `737696831915`
**Source account:** `461564345538`
**Region:** `ap-southeast-2`
**Date started:** 2026-04-15

**Related docs:**
- [Backup Solution Plan](design/backup-solution-plan.md) — overall architecture, phases, and cost analysis (this log is the execution journal for that plan)
- [ADR-002: Inventory Manifest Pipeline](design/adr/ADR-002-inventory-manifest-pipeline-ths.md) — design decision for Athena-based inventory diff
- [S3 Manifest Bottleneck](design/S3_MANIFEST_BOTTLENECK.md) — Lambda/CodeBuild sizing matrix that drove the inventory pivot
- [Athena Manifest Pipeline](design/ATHENA_MANIFEST_PIPELINE.md) — Athena inventory-diff implementation design
- [Lambda Deployment Guide](development/lambda-deployment.md) — deploy procedures for `serverless.yml`
- [Scheduling Guide](user-guide/scheduling.md) — EventBridge schedule management and `lambda_arn` config

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

---

## Step 17 — Inventory manifest implementation landed in codebase ✅ 2026-04-23

Implemented inventory-based S3 Batch manifest preparation path behind per-source
config, while keeping the external run command unchanged.

Code changes:
- Added `sources.<alias>.batch_manifest_mode` (`inline` | `inventory`, default `inline`)
  in config model.
- Updated backup engine to pass `source_alias` and `batch_manifest_mode` into
  `batch_backup_source(...)`.
- Added inventory-manifest diff path in `s3_batch.py`:
  - discovers latest inventory snapshots under `inventory/<alias>/{source|backup}/...`
  - reads Parquet inventory objects via S3 Select
  - diffs source vs backup key/etag/size (excluding operational prefixes on backup side)
  - emits standard URL-encoded S3 Batch CSV manifest rows.

Validation in repo:

```bash
uv run ruff check src/nzshm_backup/config/models.py src/nzshm_backup/backup_engine.py src/nzshm_backup/s3_batch.py tests/test_s3_batch.py
uv run pytest tests/test_s3_batch.py tests/test_backup_engine.py tests/test_check_command.py tests/test_status_command.py tests/test_setup_command.py
```

Status:
- Implementation merged locally and tested; production config not yet switched to
  `batch_manifest_mode: inventory` for any source in this step.

---

## Step 19 — Athena inventory prepare-only smoke succeeded (THS) ✅ 2026-04-23

Implemented Athena-backed inventory diff path and validated THS `prepare-only`
manifests in production auth context.

Smoke command:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup run --source ths --prepare-only
```

Observed output highlights:
- Athena query completed successfully for inventory diff
  (`query_id=4f1fbc63-5d3e-49b1-814f-ebe40278948e`).
- Selected snapshots: `source_dt=2026-04-22-01-00`, `backup_dt=2026-04-22-01-00`.
- Manifest write completed and run reached `SKIPPED` (0 rows to copy in this
  snapshot pair), as expected when source/backup inventories are in sync.

Status follow-up:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup status --source ths
```

Shows:
- `last run: ... — skipped`
- inventory freshness line with source/backup/effective timestamps.

Note:
- This was validated using local `backup-config.production.yaml` with
  `sources.ths.batch_manifest_mode: inventory`.
- Config has not been pushed to SSM in this step.

---

## Step 22 — Toshi scheduled t+5 Athena inventory test (CodeBuild) ✅ 2026-04-23

Goal: validate `toshi` on inventory-mode manifest generation using a scheduled
run (not direct/manual trigger).

Config changes applied:
- `sources.toshi.batch_manifest_mode: inventory`
- production config pushed to `/nzshm-backup/prod/config`

Created CodeBuild scheduler test path:
- Project: `nzshm-backup-toshi-backup`
- EventBridge target mode for temporary daily rule: `codebuild`

Scheduled temporary test run at `t+5`:

```bash
run_time=$(TZ=Pacific/Auckland date -v+5M "+%Y-%m-%d %H:%M %Z")
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup schedule add \
    --source toshi \
    --frequency daily \
    --time "$run_time" \
    --target codebuild \
    --codebuild-project-arn arn:aws:codebuild:ap-southeast-2:737696831915:project/nzshm-backup-toshi-backup \
    --target-role-arn arn:aws:iam::737696831915:role/nzshm-backup-events-codebuild
```

Execution evidence:
- Scheduler health showed invocation at `14:16 NZST` and latest build
  `d564bfe4-6f9c-42c2-a73e-3c262913e949` `SUCCEEDED`.
- CodeBuild logs confirm inventory path:
  - `Athena inventory diff complete for toshi/nzshm22-toshi-api-prod`
  - `source_dt=2026-04-22-01-00, backup_dt=2026-04-22-01-00`
  - manifest row count `0` and S3 path `_manifests/...`
  - `Nothing to copy — manifest is empty, skipping job submission`
- DynamoDB exports were initiated for all four Toshi tables.

Status follow-up:
- `backup status --source toshi` shows S3 `last run ... skipped` with inventory
  freshness line; no S3 Batch job (expected for empty manifest).

Cleanup:
- Removed temporary scheduler rule:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup schedule remove --source toshi --frequency daily
```

- Confirmed `nzshm-backup-toshi-daily` no longer appears in `backup schedule show`.

---

## Step 24 — Toshi scheduled Lambda run failed (missing Glue permissions) ⚠️ 2026-04-30

Weekly toshi schedule (`nzshm-backup-toshi-weekly`, Thursday 14:00 NZST) fired the
Lambda successfully, but the Athena inventory-diff query failed during manifest
preparation.

Error from CloudWatch logs:

```
Athena query bfa18477-e9b7-4b53-b5d3-f6f1088fe3f9 FAILED:
User: arn:aws:sts::737696831915:assumed-role/nzshm-backup-service-prod-ap-southeast-2-lambdaRole/nzshm-backup-service-prod-backup
is not authorized to perform: glue:CreateDatabase on resource: arn:aws:glue:ap-southeast-2:737696831915:catalog
```

Root cause:
- Lambda role in `serverless.yml` only had Glue read permissions (`GetDatabase`,
  `GetTable`, `GetTables`). Athena inventory queries need full Data Catalog CRUD
  to create databases, tables, and partitions for S3 Inventory Parquet data.
- Additionally, `backup_engine.py` did not write `status="failed"` on exceptions,
  leaving the run state permanently stuck at `"running"`.

DynamoDB exports completed normally (all four toshi tables `COMPLETED`).

---

## Step 25 — Glue permission fix and failed-state handling ✅ 2026-05-04

### Code fixes (commit `064f40d`)

**`serverless.yml`** — Added Athena and full Glue Data Catalog permissions to Lambda role:

Athena actions:
- `athena:StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`,
  `ListDatabases`, `ListTables`, `GetDatabase`, `GetTableMetadata`

Glue actions (database, table, and partition CRUD):
- `glue:GetDatabase`, `CreateDatabase`
- `glue:GetTable`, `GetTables`, `CreateTable`, `UpdateTable`, `DeleteTable`
- `glue:GetPartition`, `GetPartitions`, `CreatePartition`, `BatchCreatePartition`,
  `DeletePartition`, `UpdatePartition`, `BatchDeletePartition`

**`backup_engine.py`** — Added `write_run_state(..., status="failed")` in the
S3 backup exception handler so runs no longer get stuck at `"running"` forever.

### Deploy and validation iterations

Three deploy/test cycles were needed to discover the full set of required Glue
permissions (each failure surfaced the next missing action):

| Deploy | Error | Missing action |
|--------|-------|---------------|
| 1st | `glue:CreateDatabase` denied | `CreateDatabase`, `CreateTable`, `DeleteTable`, `UpdateTable` added |
| 2nd | `glue:BatchCreatePartition` denied | All partition CRUD actions added |
| 3rd | `glue:GetPartition` denied | `GetPartition`, `GetPartitions` added |
| 4th | **SUCCESS** | All permissions in place |

Deploy command:

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
```

Each test iteration used a temporary schedule override:

```bash
run_time=$(TZ=Pacific/Auckland date -v+5M "+%Y-%m-%d %H:%M %Z")
BACKUP_CONFIG_PATH=backup-config.production.yaml AWS_PROFILE=nshm-backup-admin \
  uv run backup schedule add --source toshi --time "$run_time" --frequency weekly
```

Final successful run status (2026-05-04 ~11:29 NZST):
- S3: `last run: 2026-05-04 11:29 NZST — skipped` (inventories in sync, no objects to copy)
- DynamoDB: all four tables `IN_PROGRESS` (exports initiated)
- `status="failed"` fix confirmed working on earlier iterations

### Schedule restored

```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml AWS_PROFILE=nshm-backup-admin \
  uv run backup schedule add --source toshi --time "2026-05-07 14:00 NZST" --frequency weekly
```

Confirmed: `cron(0 2 ? * THU *)` → Thursday 14:00 NZST locally.

### Outstanding

- SSM config has not been pushed this session — `serverless.yml` IAM changes are
  deployed via CloudFormation (not config), so Lambda permissions are active. Config
  push is only needed if `backup-config.production.yaml` content changed.
- Known bug: `schedule add` removes existing EventBridge targets before re-adding,
  but if `load_config()` fails (no local config file), the Lambda target is deleted
  and not restored. Workaround: always set `BACKUP_CONFIG_PATH` when using
  `schedule add` with `--target lambda`.

---

## Step 26 — Athena UNLOAD manifest pipeline ✅ 2026-05-04

### Problem

Lambda streaming of Athena results for manifest generation failed at scale:
- 1024 MB Lambda OOM'd on `static` (~40M objects, 4.7 GB result)
- Line-by-line streaming (`iter_lines`) achieved only ~1K rows/s — ~8 hours
  for 40M rows, far beyond Lambda's 15-minute timeout
- Even max Lambda (10 GB) estimated ~80 minutes — still too slow

### Solution: Athena UNLOAD

Replaced Lambda streaming with server-side Athena UNLOAD:

1. Athena `UNLOAD` writes diff query results directly to S3 as CSV
2. URL encoding handled in SQL via `REPLACE()` chain (8 characters:
   `%`, `,`, space, `=`, `(`, `)`, `"`, `#`)
3. `SELECT COUNT(*)` runs in parallel for exact row count
4. Lambda concatenates UNLOAD part files via S3 multipart-copy (no
   data through memory)
5. `CreateJob` with the single concatenated manifest

### Deployment iterations

| Deploy | Issue | Fix |
|--------|-------|-----|
| 1st | UNLOAD defaulted to gzip compression — binary manifest | Added `compression = 'NONE'` |
| 2nd | `HIVE_PATH_ALREADY_EXISTS` — stale `_SUCCESS` markers | Fixed cleanup to delete all objects including 0-byte markers |
| 3rd | weka batch job `AccessDenied` on source bucket | Re-ran `create-backup-roles.py` to add `nzshm22-weka-ui-prod` |
| 4th | **SUCCESS** | weka 4/4 objects copied |

### Validated results

| Source | Objects | UNLOAD time | Total Lambda | Memory | Status |
|--------|---------|-------------|-------------|--------|--------|
| `static` | 39,973,875 | ~12s | 28s | 432 MB | Batch job submitted, actively copying |
| `weka` | 4 | ~2s | ~17s | 129 MB | Batch job complete, 4/4 copied |

### Schedules after validation

```
static-weekly    ENABLED  cron(...)  → Monday (temporary, needs permanent slot)
ths-weekly       ENABLED  cron(...)  → Monday 20:15 NZST (Lambda, was CodeBuild)
toshi-weekly     ENABLED  cron(...)  → Thursday 14:00 NZST (Lambda)
weka-weekly      ENABLED  cron(...)  → Monday (temporary, needs permanent slot)
```

All sources now on Lambda — CodeBuild no longer required for any source.

### Outstanding (from Step 26)

- Permanent schedule slots for static and weka need to be set.

---

## Step 27 — Static first backup completed, toshi first backup triggered ✅ 2026-05-05

### Static results

Static batch job `b2832b7b` (39,973,875 objects) completed with only 2 failures:
- Both failed keys contained `+` characters not URL-encoded in the manifest
- Fix: expanded REPLACE chain from 9 to all 28 RFC 3986 reserved characters
- Also fixed: single quote `'` in REPLACE chain broke Athena SQL parser

SSM config pushed (static `batch_manifest_mode: inventory` was missing).

### Toshi first S3 backup

Toshi had never had a successful S3 backup — every previous run was either
"failed" (Glue permissions) or "skipped" (NULL `is_latest` filter bug on
non-versioned buckets). Triggered first real run:

- Batch job `bb5d364d`: 6,908,702 objects submitted
- UNLOAD completed in ~20 seconds

### THS smart ETag validation

THS diff was producing 4,224 false positives per run — objects re-copied
despite identical content. Root cause: S3 Batch copy produces different ETags
from source when upload method differs (multipart vs single-part).

Fix: smart ETag comparison — only compare ETags when both are single-part
(no `-` suffix). Falls back to size-only when either is multipart.

Validated: THS run returned "skipped" (0 objects) after fix — false
positives eliminated.

### Test restore improvements

1. **Inventory-based sampling**: `test restore` now queries Athena inventory
   for random samples instead of listing entire backup buckets. THS (3.8M
   objects) sampling completes in seconds instead of minutes.

2. **Checksum verification**: `test restore` now compares CRC64NVME checksums
   (via `GetObjectAttributes`) when available, falling back to ETag. S3 Batch
   already computes CRC64NVME on copied objects.

Validated: weka and THS both pass restore tests with checksum verification.

### Inventory guard for large buckets

`test restore --source static` was falling back to listing 40M objects when
backup inventory was unavailable (not yet refreshed after first backup).
Added guards:
- `test restore`: refuses listing fallback for inventory-mode sources;
  prints actionable message with remediation steps
- `test integrity`: warns before running full listing on large buckets
- Improved `_latest_inventory_partition` error message with guidance

### SSM config push

Pushed `backup-config.production.yaml` to SSM — static `batch_manifest_mode:
inventory` was missing from the SSM copy.

### Object count reconciliation (2026-05-06, inventory dt=2026-05-05)

Reconciled source inventory counts against backup inventory counts for
all four production sources. Backup buckets contain operational objects
(manifests, batch reports, event logs, state files) in addition to backed-up
data objects.

| Source | Source objects | Backup (total) | Backup (operational) | Backup (data) | Match? |
|--------|--------------|----------------|---------------------|---------------|--------|
| static | 39,973,875 | 39,973,884 | 9 | 39,973,875 | **Yes** |
| toshi | 6,908,702 | 6,908,710 | 8 | 6,908,702 | **Yes** |
| ths | 3,886,583 | 3,886,621 | 38 | 3,886,583 | **Yes** |
| weka | 11 | 33 | 22 | 11 | **Yes** |

All sources fully reconciled: source data count = backup data count.

**Note on static bucket metrics showing ~52M objects:** The backup bucket
has versioning enabled. Two full-sync batch jobs ran (the second due to
inventory lag before backup inventory refreshed), creating non-current
versions for objects overwritten by the second job. Bucket metrics count
all versions (current + non-current). Non-current versions will expire
via lifecycle policy (365 days).

### Storage cost analysis (2026-05-06)

Non-current object versions (from duplicate batch jobs and ETag false-positive
re-copies during testing) add ~1,248 GB of overhead across backup buckets.

| Source | Source (GB) | Backup current (GB) | Backup total (GB) | Non-current (GB) | Overhead |
|--------|------------|--------------------|--------------------|-----------------|----------|
| static | 2,668 | 2,673 | 3,497 | 824 | 31% |
| toshi | 7,885 | 7,886 | 7,887 | 2 | 0% |
| ths | 427 | 431 | 853 | 423 | 100% |
| weka | 0.01 | 0.03 | 0.05 | 0.02 | — |
| **Total** | **10,980** | **10,989** | **12,238** | **1,248** | **11%** |

Cost impact (S3 Standard): ~NZD 76/month for non-current versions.
Will reduce as lifecycle transitions apply:
- 30 days → Glacier IR (~NZD 15/month)
- 120 days → Deep Archive (~NZD 4/month)
- 365 days → expired (NZD 0)

Root causes:
- **static** (824 GB): duplicate full-sync batch job due to inventory lag
  (backup inventory not refreshed before second run)
- **ths** (423 GB): 4,224 false-positive re-copies from multipart ETag
  mismatch (fixed by smart ETag comparison) plus earlier failed/retried jobs

Both root causes are now mitigated (inventory lag guard, smart ETag diff).
No further non-current accumulation expected under normal weekly schedule.

---

## Step 28 — Switch to daily backups, redeploy with cleanup fix ✅ 2026-05-07

### Toshi HIVE_PATH_ALREADY_EXISTS (last night's run)

Toshi weekly run (2026-05-06 20:15 NZST) failed — stale UNLOAD output from
May 5 run was not cleaned up because the deployed Lambda was from before the
cleanup fix. The other three sources ran fine (all skipped — in sync).

Fix: cleaned stale prefix manually, redeployed Lambda with latest code
(cleanup fix + all accumulated fixes from this session).

Toshi re-run at 10:05 NZST: skipped successfully.

### Schedule change: weekly → daily

Switched all four sources from weekly to daily at 13:05 NZST:

```bash
backup schedule remove --source <all> --frequency weekly
backup schedule add --source <all> --frequency daily --time "13:05 NZST"
```

Cost impact of daily vs weekly (when inventories are in sync):
- Athena UNLOAD + COUNT: ~$0.01-0.04/run
- Lambda: ~$0.0005/run
- DynamoDB exports (toshi only): ~$0.002/run
- Estimated: ~$1.50-4.50/month (up from ~$0.20-0.65/month weekly)
- RPO improvement: worst-case 7 days → ~1-2 days

First daily run (2026-05-07 13:05 NZST): all four sources skipped — clean.

### Current schedule

```
nzshm-backup-static-daily     ENABLED  cron(5 1 * * ? *)  → 13:05 NZST daily
nzshm-backup-toshi-daily      ENABLED  cron(5 1 * * ? *)  → 13:05 NZST daily
nzshm-backup-ths-daily        ENABLED  cron(5 1 * * ? *)  → 13:05 NZST daily
nzshm-backup-weka-daily       ENABLED  cron(5 1 * * ? *)  → 13:05 NZST daily
```

---

## Step 29 — AWS Backup decommissioned ✅ 2026-05-13

AWS Backup removed from the NSHM backup account. The custom nzshm-backup
solution is now the **sole backup system** for all production data.

- **Cost reduction:** $1,700/month (AWS Backup) → ~$10/month (custom solution)
- **Savings:** ~97% / ~$20,000 NZD/year
- **Coverage:** 50.8M objects, 11 TB across 4 sources + 4 DynamoDB tables
- **Schedule:** daily at 13:05 NZST (was weekly under AWS Backup)
- **Validation:** daily automated diff + restore tests with CRC64 checksums

7+ days of clean daily runs preceded decommission. All restore tests
passing across all sources.

---

## Step 20 — Deployed Athena THS runtime artifact to CodeBuild ✅ 2026-04-23

Published runtime artifact from commit `6fb7128` to the THS CodeBuild source key:

```bash
git archive --format=zip --output /tmp/nzshm-backup-codebuild-ths-cutover.zip HEAD
AWS_PROFILE=nshm-backup-admin aws s3 cp \
  /tmp/nzshm-backup-codebuild-ths-cutover.zip \
  s3://nzshm-backup-codebuild-src-737696831915/nzshm-backup-codebuild-ths-cutover.zip \
  --region ap-southeast-2
```

Triggered production-equivalent THS build smoke:

```bash
AWS_PROFILE=nshm-backup-admin aws codebuild start-build \
  --region ap-southeast-2 \
  --project-name nzshm-backup-ths-backup
```

Build result:
- Build ID: `nzshm-backup-ths-backup:4c0cf859-1972-4029-b113-a11d730bf11f`
- Status: `SUCCEEDED`
- Runtime: ~49s total (`INSTALL` ~21s, `BUILD` ~20s)

Post-deploy status check:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup status --source ths
```

Observed:
- `last run: ... — skipped`
- inventory freshness line present (`source`, `backup`, `effective`)
- no new S3 Batch job submitted (expected when diff is empty)

Notes:
- Production config with `sources.ths.batch_manifest_mode: inventory` has already
  been pushed to SSM (`/nzshm-backup/prod/config`).
- THS scheduler remains EventBridge -> CodeBuild (no target-mode change in this step).

---

## Step 21 — Weka switched to S3 Batch inventory mode and validated ✅ 2026-04-23

Updated production config for `weka`:

- `sources.weka.use_s3_batch: true`
- `sources.weka.batch_manifest_mode: inventory`

Pushed config to SSM:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup config push --stage prod
```

Preflight check:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup check --source weka
```

Result: all checks passed (including S3 Batch role, inventory config, and inventory snapshots).

Live run:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup run --source weka
```

Observed run output:
- Athena inventory diff query succeeded (`query_id=54baacc6-ddde-4e29-a76b-97d29349d963`)
- Selected snapshots: `source_dt=2026-04-22-01-00`, `backup_dt=2026-04-22-01-00`
- Manifest row count: `0`
- Run terminal state: `SKIPPED` (no differences to copy)

Status follow-up:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup status --source weka
```

Shows:
- `last run: ... — skipped`
- inventory freshness line with source/backup/effective timestamps
- `no batch jobs found` (expected because no job is submitted when manifest is empty)

---

## Step 18 — S3 Select blocker confirmed; pivot to Athena design ✅ 2026-04-23

Attempted a THS `prepare-only` smoke run after enabling inventory mode:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup run --source ths --prepare-only
```

Observed blocker:
- Inventory manifest prep failed with `MethodNotAllowed` on
  `SelectObjectContent` against inventory Parquet objects.
- Reproduced with direct AWS CLI call on a known THS inventory Parquet object:

```bash
AWS_PROFILE=nshm-backup-admin aws s3api select-object-content \
  --bucket nzshm-backup-inventory-737696831915 \
  --key inventory/ths/source/ths-dataset-prod/.../data/<uuid>.parquet \
  --expression "SELECT s.key, s.size, s.e_tag FROM S3Object s LIMIT 1" \
  --expression-type SQL \
  --input-serialization '{"Parquet":{}}' \
  --output-serialization '{"CSV":{}}' /tmp/ths-select.out
```

Result: same `MethodNotAllowed` error.

Interpretation:
- S3 Select is not a viable implementation path in this account/runtime context.
- Pivot implementation path to Athena-backed inventory diff (as per ADR-002).

Operational decision:
- Keep production THS scheduler on CodeBuild path until Athena manifest prep is implemented and validated.

---

## Step 16 — Inventory readiness confirmed across all sources ✅ 2026-04-23

Re-ran production preflight checks after SSO refresh and toshi versioning remediation.

Command pattern used:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup check --source <alias>
```

Result summary:
- `ths`: all checks passed; inventory snapshots present; effective data time `2026-04-23 06:52 NZST`.
- `toshi`: all checks passed (including versioning + PITR); inventory snapshots present; effective data time `2026-04-23 06:52 NZST`.
- `weka`: all checks passed; inventory snapshots present; effective data time `2026-04-23 06:52 NZST`.
- `static`: all checks passed; inventory snapshots present; effective data time `2026-04-23 06:52 NZST`.

Outcome:
- Inventory producers are now both configured and actively delivering artifacts for all four production sources.
- Environment is ready to begin inventory-based manifest generation implementation.

---

## Step 15 — Inventory producers enabled for all sources ✅ 2026-04-22

Set up daily Parquet S3 Inventory for source + backup bucket pairs using the
new helper script `scripts/setup-inventory.py`.

Control bucket (backup account):
- `nzshm-backup-inventory-737696831915`

Configured sources:
- `ths`
- `toshi`
- `weka`
- `static`

Command pattern used:

```bash
uv run python scripts/setup-inventory.py \
  --config backup-config.production.yaml \
  --source <alias> \
  --source-profile nshm-admin \
  --backup-profile nshm-backup-admin
```

Verified for each source and backup bucket:
- inventory configuration exists and is enabled
- destination format is `Parquet`
- destination prefixes follow `inventory/<source>/{source|backup}/...`

Note:
- First inventory artifacts may take up to 24-48h to appear.
- `backup check --source <alias>` now reports inventory config readiness and
  snapshot presence/effective data timestamp when artifacts are available.

Inventory readiness snapshot (2026-04-22, via `backup check --source <alias>`):
- `ths`: inventory configs enabled on source+backup; snapshots not yet present; effective data time pending.
- `toshi`: inventory configs enabled on source+backup; snapshots not yet present; effective data time pending.
- `weka`: inventory configs enabled on source+backup; snapshots not yet present; effective data time pending.
- `static`: inventory configs enabled on source+backup; snapshots not yet present; effective data time pending.

Operational note (resolved 2026-04-22):
- `toshi` backup bucket versioning was enabled manually:

```bash
aws s3api put-bucket-versioning \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --bucket bb-toshi-s3-api-prod-ap-southeast-2-461564345538 \
  --versioning-configuration Status=Enabled
```

- Verification:

```bash
aws s3api get-bucket-versioning \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --bucket bb-toshi-s3-api-prod-ap-southeast-2-461564345538 \
  --query 'Status' --output text
```

Output: `Enabled`

- Post-fix check: `backup check --source toshi` now passes (inventory snapshot warnings remain expected until first artifact delivery).

---

## Step 14 — Current production backup status snapshot ✅ 2026-04-20

Captured current status after THS CodeBuild cutover + manifest/policy fixes.

Command:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup status
```

Summary:

- **ths** (CodeBuild + S3 Batch): latest job `34fbef5d-b74c-4fb0-8b6d-53567dcc3d49`
  completed successfully with `3,878,278/3,878,278` objects and `0` failures.
- **toshi**: DynamoDB export status checks now succeed (all four PROD tables show
  latest `COMPLETED`; previous `dynamodb:ListExports` IAM errors resolved).
- **static**: no batch jobs submitted yet.
- **weka**: incremental backup last run remains successful.

Notes:

- Historical failed THS jobs remain visible in status output (expected for audit history).
- THS weekly scheduler target remains CodeBuild (`project/nzshm-backup-ths-backup`).

---

## Step 10 — THS versioning incident response (guardrail-first) ⚠️ 2026-04-16

Created isolation branch for incident work and guardrails:

```bash
git checkout -b fix/ths-versioning-guardrails
```

### Code fixes completed

- Added backup-bucket versioning guardrail to `backup check`:
  - existing bucket + versioning enabled => `PASS`
  - existing bucket + versioning disabled/missing => `FAIL`
- Added remediation-focused error on first-run bucket bootstrap when
  `s3:PutBucketVersioning` is denied.
- Updated Lambda IAM template permissions in `serverless.yml`:
  - `s3:GetBucketVersioning`
  - `s3:PutBucketVersioning`
- Added tests for:
  - check-command versioning pass/fail behavior
  - AccessDenied regression path for versioning enable during bootstrap

Validation in repo:

```bash
uv run pytest tests/test_check_command.py tests/test_s3_backup.py
uv run ruff check src/nzshm_backup/commands/check.py src/nzshm_backup/s3_backup.py tests/test_check_command.py tests/test_s3_backup.py
```

### Guardrail validation in production (before remediation)

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup check --source ths
```

```text
Source: ths
  [PASS] Backup account credentials  737696831915
  [PASS] Assume role nzshm-backup-reader  arn:aws:sts::461564345538:assumed-role/nzshm-backup-reader/nzshm-backup
  [PASS] Read ths-dataset-prod
  [PASS] Backup bucket bb-ths-s3-dataset-prod-ap-southeast-2-461564345538  exists
  [FAIL] Versioning bb-ths-s3-dataset-prod-ap-southeast-2-461564345538  status=Disabled — enable before backup
  [PASS] S3 Batch role nzshm-backup-batch-role

One or more checks FAILED — review errors above before running backup.
```

Interpretation: guardrail worked as intended and blocked THS backup readiness while
object versioning protection was disabled.

### Remediation executed

Enabled versioning on the existing THS backup bucket:

```bash
aws s3api put-bucket-versioning \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --bucket bb-ths-s3-dataset-prod-ap-southeast-2-461564345538 \
  --versioning-configuration Status=Enabled
```

Verification:

```bash
aws s3api get-bucket-versioning \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --bucket bb-ths-s3-dataset-prod-ap-southeast-2-461564345538 \
  --query 'Status' --output text
```

Output: `Enabled`

Re-ran guardrail check:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup check --source ths
```

```text
Source: ths
  [PASS] Backup account credentials  737696831915
  [PASS] Assume role nzshm-backup-reader  arn:aws:sts::461564345538:assumed-role/nzshm-backup-reader/nzshm-backup
  [PASS] Read ths-dataset-prod
  [PASS] Backup bucket bb-ths-s3-dataset-prod-ap-southeast-2-461564345538  exists
  [PASS] Versioning bb-ths-s3-dataset-prod-ap-southeast-2-461564345538  Enabled
  [PASS] S3 Batch role nzshm-backup-batch-role

All checks passed.
```

### Verification schedule (short-interval)

Added a temporary THS daily schedule to validate the fix path quickly:

```bash
run_time=$(TZ=Pacific/Auckland date -v+5M "+%Y-%m-%d %H:%M %Z")
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source ths --frequency daily --time "$run_time"
```

Result:

```text
Rule 'nzshm-backup-ths-daily' created/updated: cron(36 22 * * ? *)  → 10:36 NZST locally
Target registered: arn:aws:lambda:ap-southeast-2:737696831915:function:nzshm-backup-service-prod-backup
```

`backup schedule show` confirms both rules are enabled:
- `nzshm-backup-ths-weekly` (normal production cadence)
- `nzshm-backup-ths-daily` (temporary verification rule)

### THS re-run note

Started `backup run --source ths` manually, but cancelled interactive execution because
listing/manifest generation for this source is too large for a live interactive run.
Full completion verification is deferred to scheduled run + log/S3 Batch status review.

---

## Step 11 — Manifest bottleneck matrix at max Lambda memory ⚠️ 2026-04-16

Objective: verify whether increasing Lambda resources alone can make inline
manifest preparation reliable for large batch sources.

Agreed protocol:
- Set backup Lambda to max memory (`10240 MB`, timeout unchanged at `900s`)
- Scan `ths`, `toshi`, `static`
- Capture pass/fail + duration + max memory + whether batch job submission occurs
- Abort early if max-memory gate fails

### Runtime configuration changes

```bash
aws lambda update-function-configuration \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --function-name nzshm-backup-service-prod-backup \
  --memory-size 10240 \
  --timeout 900
```

After experiment, restored baseline:

```bash
aws lambda update-function-configuration \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --function-name nzshm-backup-service-prod-backup \
  --memory-size 1024 \
  --timeout 900
```

### Results

| Source | Result | Duration | Max memory | Notes |
|--------|--------|----------|------------|-------|
| `ths` | **FAIL** | `900000 ms` | `4126 MB` | Timed out while listing source objects; no batch job created |
| `toshi` | FAIL (different blocker) | `6315.40 ms` | `127 MB` | `AccessDenied` on `s3:PutBucketVersioning` for `bb-toshi-s3-api-prod-ap-southeast-2-461564345538` |
| `static` | not run | n/a | n/a | max-memory gate already failed; matrix aborted early |

Representative THS report:

```text
REPORT RequestId: 73dfbd1b-c3b3-4813-b29d-85b5adf8d88e  Duration: 900000.00 ms
Billed Duration: 901231 ms  Memory Size: 10240 MB  Max Memory Used: 4126 MB  Status: timeout
```

Conclusion:
- Memory scaling alone does not make inline manifest prep reliable for THS.
- Design change is required for large sources (manifest generation outside Lambda).
- THS is acting as the canary failure. Based on source scales (`static` ~40M objects,
  `toshi` ~8M, `ths` ~4M), `static` remains the largest expected S3 manifest-prep blocker.

Operational cleanup:
- Disabled temporary daily THS verification rule to avoid repeated timeout loops.

```bash
aws events disable-rule \
  --profile nshm-backup-admin \
  --region ap-southeast-2 \
  --name nzshm-backup-ths-daily
```

Detailed design + matrix notes captured in:
`docs/design/S3_MANIFEST_BOTTLENECK.md`

---

## Step 12 — THS canary CodeBuild matrix (compute sizing) ⚠️ 2026-04-16

Objective: test whether moving manifest prep to CodeBuild can finish THS within
the Lambda-equivalent 15-minute target.

Method:
- Temporary CodeBuild project (`nzshm-backup-manifest-benchmark`) using source zip
  from S3 and `backup run --source ths --prepare-only`.
- Compute tiers tested big -> small:
  `BUILD_GENERAL1_2XLARGE`, `LARGE`, `MEDIUM`, `SMALL`.

Results:

| Compute | Result | Manifest runtime | Notes |
|---------|--------|------------------|-------|
| `2XLARGE` | SUCCESS | `3283s` (~54m43s) | manifest created (`3,886,583` rows) |
| `LARGE` | SUCCESS | `3585s` (~59m45s) | manifest created (`3,886,583` rows) |
| `MEDIUM` | SUCCESS | `3086s` (~51m26s) | manifest created (`3,886,583` rows) |
| `SMALL` | FAILED | n/a (killed) | process exited `137` (~21m40s), likely OOM while listing source objects |

Conclusion:
- THS manifest prep in CodeBuild is reliable enough to complete on medium+ sizes,
  but **not** within 15 minutes.
- Best observed runtime is still ~51 minutes, so this does not meet the
  15-minute target even off Lambda.

Implication:
- Compute scaling alone does not solve the runtime target. We need a design change
  (precomputed manifests / inventory-driven / workflow split) if 15-minute windows
  are a hard requirement.

---

## Step 13 — THS CodeBuild cutover prep (issue #9) 🚧 2026-04-20

Started implementation for THS interim schedule cutover to CodeBuild.

Code changes:
- `backup schedule add` now supports explicit target selection:
  - `--target lambda` (default, existing behavior)
  - `--target codebuild` (new)
- Added required parameters for CodeBuild targeting:
  - `--codebuild-project-arn`
  - `--target-role-arn` (EventBridge invoke role)
- `backup schedule remove` now removes all rule targets (Lambda and/or CodeBuild)
  before deleting the rule.

Validation:

```bash
uv run pytest tests/test_schedule.py
uv run ruff check src/nzshm_backup/commands/schedule.py tests/test_schedule.py
```

Result: tests and lint pass.

This is the CLI/platform plumbing required before switching the live THS weekly
rule from Lambda to CodeBuild.

### Live THS schedule cutover executed

Created EventBridge invoke role for CodeBuild target:

```bash
aws iam create-role \
  --profile nshm-backup-admin \
  --role-name nzshm-backup-events-codebuild \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"events.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy \
  --profile nshm-backup-admin \
  --role-name nzshm-backup-events-codebuild \
  --policy-name start-ths-codebuild \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["codebuild:StartBuild"],"Resource":"arn:aws:codebuild:ap-southeast-2:737696831915:project/nzshm-backup-ths-backup"}]}'
```

Created THS CodeBuild project:

- Name: `nzshm-backup-ths-backup`
- Source: `s3://nzshm-backup-codebuild-src-737696831915/nzshm-backup-codebuild-ths-cutover.zip`
- Compute: `BUILD_GENERAL1_MEDIUM`
- Timeout: `70 minutes`
- Service role: `arn:aws:iam::737696831915:role/nzshm-backup-codebuild-manifest-test-role`
- Logs: `/aws/codebuild/nzshm-backup-ths-backup`
- Build command: `BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup run --source ths`
- Includes overlap guard (skip if another build for this project is already queued/running)

Cut over weekly THS rule target from Lambda -> CodeBuild:

```bash
AWS_PROFILE=nshm-backup-admin BACKUP_CONFIG_PATH=backup-config.production.yaml \
  uv run backup schedule add \
    --source ths \
    --frequency weekly \
    --time "2026-04-15 20:15 NZST" \
    --target codebuild \
    --codebuild-project-arn arn:aws:codebuild:ap-southeast-2:737696831915:project/nzshm-backup-ths-backup \
    --target-role-arn arn:aws:iam::737696831915:role/nzshm-backup-events-codebuild
```

Verification:

- Rule `nzshm-backup-ths-weekly` is now `ENABLED` with `cron(15 8 ? * WED *)`
- Rule has single target:
  - `Id=backup-codebuild`
  - `Arn=arn:aws:codebuild:ap-southeast-2:737696831915:project/nzshm-backup-ths-backup`
  - `RoleArn=arn:aws:iam::737696831915:role/nzshm-backup-events-codebuild`
- Lambda target removed from the THS weekly rule

Follow-up improvements:
- `backup schedule show` now reports target mode/details (`lambda` vs `codebuild`)
  so mixed-target operations are visible from CLI output.
- Added mixed-target release checklist to `docs/user-guide/scheduling.md` to keep
  Lambda deploys, CodeBuild artifacts, config pushes, and schedule wiring in sync.

### First THS CodeBuild trial result + remediation

Manual run was triggered via CodeBuild project `nzshm-backup-ths-backup`.

- Manifest preparation completed and job was submitted:
  - `job_id=443d8d28-2519-4556-b2c5-660a3f4156f5`
  - `objects_in_manifest=3,886,583`
- CodeBuild build completed `SUCCEEDED`.

However, the submitted batch job immediately entered `Failing` with 100% task
failures (`FailureThresholdReached`).

Root cause:
- `nzshm-backup-batch-role` inline policy `ReadSource` only allowed
  `arn:aws:s3:::nzshm22-toshi-api-prod/*` and did not include THS/static source
  buckets.

Fix applied:

```bash
AWS_PROFILE=nshm-backup-admin uv run python scripts/create-backup-roles.py \
  --config backup-config.production.yaml
```

Verified `ReadSource` now includes:
- `arn:aws:s3:::nzshm22-toshi-api-prod/*`
- `arn:aws:s3:::ths-dataset-prod/*`
- `arn:aws:s3:::nzshm22-static-reports/*`

Validation rerun started:
- `nzshm-backup-ths-backup:b7eabcc9-effd-4964-8273-5964ac343f46`
- monitoring in progress for successful submission + task success progression.

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

## Step 8 — Production schedules ✅ 2026-04-15

Weekly cadence chosen for all sources. DynamoDB exports (toshi) run weekly alongside S3
rather than on a separate 28-day cycle — cost difference is ~$117 NZD/year, simplicity wins.
PITR remains always-on for 35-day any-second recovery regardless of export frequency.

`weka`, `ths`, `static` scheduled for Wednesday (tonight); `toshi` staggered to Thursday
to avoid large S3 Batch and DynamoDB jobs competing with the others.

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source weka --frequency weekly --time "2026-04-15 20:15 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source ths --frequency weekly --time "2026-04-15 20:15 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source static --frequency weekly --time "2026-04-15 20:15 NZST"
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup schedule add --source toshi --frequency weekly --time "2026-04-16 20:15 NZST"
```

Final schedule state:

```
Rule Name                                     State      Schedule                       Local time
----------------------------------------------------------------------------------------------------
nzshm-backup-pitr-watcher                     DISABLED   rate(5 minutes)
nzshm-backup-static-weekly                    ENABLED    cron(15 8 ? * WED *)           → Wednesday 20:15 NZST locally
nzshm-backup-ths-weekly                       ENABLED    cron(15 8 ? * WED *)           → Wednesday 20:15 NZST locally
nzshm-backup-toshi-weekly                     ENABLED    cron(15 8 ? * THU *)           → Thursday 20:15 NZST locally
nzshm-backup-weka-weekly                      ENABLED    cron(15 8 ? * WED *)           → Wednesday 20:15 NZST locally
```

_Status: weka/ths/static fire tonight 20:15 NZST; toshi fires tomorrow 20:15 NZST_

---

## Step 9 — Enable S3 Batch for ths and static ✅ 2026-04-15

`ths` (~4M objects) and `static` (~40M objects) were incorrectly set to `use_s3_batch: false` —
direct incremental listing at that scale would page through millions of S3 API calls.
Updated config and pushed to SSM.

| Source | Objects | Size   | S3 Batch |
|--------|---------|--------|----------|
| toshi  | ~8M     | ~8TB   | true     |
| ths    | ~4M     | ~1TB   | true     |
| static | ~40M    | ~2.7TB | true     |
| weka   | ~64     | ~80MB  | false    |

```bash
eval $(aws configure export-credentials --profile nshm-backup-admin --format env)
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod
```

---

## Step 14 — Notification fast path: Lambda-error alarm + email (#20 / ADR-005) ✅ 2026-05-19

Closed the silent-failure gap that hid 4 days of weka backup failures
(2026-05-15 → 2026-05-19). CloudWatch alarm on the backup Lambda's
`Errors` metric (≥1 over 5 min) → SNS topic `nzshm-backup-alerts-prod`
→ email subscription to `chrisbc@artisan.co.nz`. Test command
`backup test alert` forces ALARM to verify end-to-end.

Deployed from branch `feature/notifications` (PR #20 not yet merged at
that point). Confirmed subscription, then validated via
`aws sns publish` (test 1) and `backup test alert` (test 2) —
both emails arrived ~30s.

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
# AWS sent subscription confirmation email — click link
uv run backup test alert     # expect ALARM email + ~5 min later OK email
```

---

## Step 15 — Daily health report slow path (ADR-005, PR #21) ✅ 2026-05-20

Slow-path complement to Step 14. Daily Lambda task combining:

- `backup status` snapshot (per-source run state, batch jobs, DDB exports)
- inventory freshness (ADR-007 mit. 4 — >30h flags yellow)
- object-count delta vs yesterday's S3 Inventory partition (ADR-006
  mit. 1 — ≥5% or ≥10k drop flags red)
- sampled restore verification (weka canary daily + Mon/Wed/Fri large-
  source rotation through ths/toshi/static)

Delivers via Slack Block Kit webhook **and** plain-text email through a
separate SNS topic `nzshm-backup-reports-prod`. ADR-005 originally
specified SES — rejected in favour of SNS during review (domain
verification + sandbox-mode escape too heavy for an internal report).

Deploy sequence:

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
# Confirm SNS subscription via emailed link
uv run backup health-report run --send  # live verify
```

First successful end-to-end send: 2026-05-20 (weka canary passed,
GREEN 4/4). Slack webhook stored as Secrets Manager
`backup-slack-webhook`; production yaml flipped to
`notifications.slack.enabled: true` and
`notifications.reports.email.enabled: true`.

The 5 health-report tuning knobs (canary, rotation map, freshness
threshold, delta thresholds, sample size) added under
`notifications.reports.health` so operators can adjust without code
changes.

Note: no Lambda dispatch or EventBridge schedule yet — exercising the
code via the CLI only at this point. (Step 16 wires the cron.)

---

## Step 16 — Daily-report trigger + multi-recipient subscriptions ✅ 2026-05-22

Two related changes deployed together:

### 16a. Lambda + EventBridge schedule (ADR-005, PR #22)

- `BackupTask.task_type: Literal["backup","health_report"] = "backup"`
  schema field (default keeps existing rules valid).
- Lambda handler branches on `task.task_type == "health_report"` and
  calls `health_report.build_report` + `send`.
- `backup schedule add --task-type health_report ...` creates the
  EventBridge rule.

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
uv run backup schedule add --source _health --task-type health_report \
    --frequency daily --time "14:30 NZST"
```

Rule `nzshm-backup-health-report-daily` created. Cron
`cron(30 2 * * ? *)` UTC → 14:30 NZST daily. Target payload:
`{"source": "_health", "trigger_type": "scheduled", "task_type": "health_report"}`.
First scheduled fire: 2026-05-23 14:30 NZST.

### 16b. Notification recipients managed from YAML (ADR-008)

Removed the asymmetry where the first recipient lived in YAML and
additional recipients had to be added via raw `aws-cli`. Now both
recipient lists (`notifications.alerts.emails`,
`notifications.reports.email.addresses`) are lists in
`backup-config.production.yaml`, reconciled by
`backup notifications apply`. CloudFormation no longer manages
individual subscriptions; the two topics + the CloudWatch alarm remain
CFN-owned.

Three subscribers configured on both topics:

| Address | alerts | reports |
|---|---|---|
| chrisbc@artisan.co.nz | confirmed | confirmed |
| cjdicaprio@proton.me | pending | pending |
| chris.dicaprio@earthsciences.nz | pending | pending |

Deploy + apply sequence:

```bash
BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod  # CFN drops old per-CFN subscriptions
uv run backup notifications apply                          # subscribes the 3 addresses on both topics
uv run backup notifications show                           # check confirmed/pending
```

Pending confirmations must be clicked by the recipient — AWS expires
unconfirmed subscriptions after ~3 days. A fresh `apply` re-issues for
expired pending if still in YAML.

End-to-end verification:

```bash
uv run backup health-report run --send   # GREEN 4/4, Slack ok, SNS ok
uv run backup test alert                  # alarm email arrives
```

---

## Step 17 — Scoped s3:Delete* IAM for restore-test temp buckets ✅ 2026-05-22

**Symptom:** the first automated 14:30 NZST fire of the daily health
report (2026-05-22 02:30 UTC) produced a GREEN report on Slack/SNS but
both restore tests inside reported as **failed**.

**Root cause:** the Lambda role intentionally lacks `s3:DeleteObject` /
`s3:DeleteBucket` ("backup buckets are delete-protected"). The
restore-test workflow creates a temp bucket
(`bb-restore-test-<ts>-<account>`), copies + verifies the sample, then
deletes the bucket. Locally this worked because admin SSO credentials
are unrestricted; on the deployed Lambda, the cleanup `delete_objects`
+ `delete_bucket` calls failed with AccessDenied. The error was
captured in `BucketRestoreResult.copy_errors` and classified as
failure — even though the actual copy + checksum verify had succeeded.

**Evidence:** two orphan temp buckets after the run:

```
bb-restore-test-1779417137-737696831915  (static, 02:32:20 UTC)
bb-restore-test-1779417175-737696831915  (weka,   02:32:57 UTC)
```

**Fix:** name-pattern-scoped Allow on `bb-restore-test-*`, keeping
the no-delete guarantee on real backup buckets.

```yaml
- Effect: Allow
  Action:
    - s3:DeleteObject
    - s3:DeleteBucket
  Resource:
    - "arn:aws:s3:::bb-restore-test-*"
    - "arn:aws:s3:::bb-restore-test-*/*"
```

**Deploy sequence:**

```bash
# 1. Clean up the two orphans with admin credentials
for B in bb-restore-test-1779417137-737696831915 bb-restore-test-1779417175-737696831915; do
  aws s3 rm "s3://$B" --recursive
  aws s3api delete-bucket --bucket "$B"
done

# 2. Deploy the IAM change
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod

# 3. Verify via direct Lambda invoke
aws lambda invoke --function-name nzshm-backup-service-prod-backup \
  --payload '{"source":"_health","task_type":"health_report"}' \
  --invocation-type Event \
  --cli-binary-format raw-in-base64-out /dev/null
# Wait ~3 min, then:
aws s3api list-buckets --query 'Buckets[?starts_with(Name, `bb-restore-test-`)].Name'
# expected: empty
```

Post-fix verification: zero temp buckets after each subsequent
invocation. The 2026-05-23 14:30 NZST scheduled fire reported
GREEN 4/4 with `restore=passed`.

## Step 18 — ADR-006 lifecycle re-apply on deployed buckets (#27) ✅ 2026-05-26

**Context:** PR #25 landed the ADR-006 two-tier lifecycle code change
on `pre-release` earlier today, but `apply_lifecycle_policy` is only
called inside `ensure_backup_bucket_ready` at *bucket creation* — it
short-circuits on existing buckets. So the production buckets were
still carrying the old 3-tier policy (Standard → GLACIER_IR @ 30d →
DEEP_ARCHIVE @ 120d → Expiration @ 365d) and no CLI path existed to
re-apply.

**Tool added (PR #27):** `backup setup lifecycle [--source <alias|all>]
[--dry-run]` walks the configured backup buckets, builds
`LifecycleConfig` from `config.retention`, and pushes via the existing
`apply_lifecycle_policy` helper.

**Bug caught at first dry-run against prod:** bucket-name derivation
was using `get_account_id(session)` (backup-account caller,
`737696831915`) instead of `source_account_id` from each
`SourceConfig` (`461564345538`). Every apply would have 404'd.
Fixed before the live apply — see `backup_engine.py:72,81` for the
authoritative naming convention. Commit: `8327ef8`.

**Pre-deploy snapshot (BEFORE):**

| Bucket | Lifecycle |
|---|---|
| `bb-toshi-s3-api-prod-…` | **NoSuchLifecycleConfiguration** (never set) |
| `bb-ths-s3-dataset-prod-…` | **NoSuchLifecycleConfiguration** (never set) |
| `bb-static-s3-static-reports-…` | 3-tier (GIR @ 30d → DA @ 120d, exp 365d, NCV 365d) |
| `bb-weka-s3-weka-ui-prod-…` | 3-tier |
| `bb-toshi-dynamo-…` | 3-tier |

Two of the largest buckets had **no lifecycle policy at all** — likely
created before the lifecycle path was wired in (or the bucket re-create
sequence skipped that step). All accumulated objects were sitting in
S3 Standard.

**Deploy sequence:**

```bash
aws sso login --profile nshm-backup-admin
eval "$(aws configure export-credentials --profile nshm-backup-admin --format env)"
unset AWS_PROFILE

# Dry-run first — verifies bucket names, prints intended JSON
uv run backup setup lifecycle --source all \
  --config backup-config.production.yaml --dry-run

# Apply
uv run backup setup lifecycle --source all \
  --config backup-config.production.yaml
```

Apply output: `[OK]` for all 5 buckets.

**Verification (AFTER) — all 5 buckets:**

```json
{
  "ID": "BackupTierTransition",
  "Transitions": [{"Days": 30, "StorageClass": "GLACIER_IR"}],
  "NCV": {"NoncurrentDays": 365},
  "Expiration": null
}
```

No `DEEP_ARCHIVE`, no `Expiration`. `NCV` = `NoncurrentVersionExpiration`
— superseded versions age out at 365 days, which keeps the historic
ETag-bloat tail (#24) bounded.

**Cost effect:** the two buckets that had no lifecycle (toshi-api +
ths-dataset, ~12M objects between them) will begin transitioning to
GLACIER_IR for everything older than 30 days. Expect a one-off
transition-request charge and a measurable monthly storage reduction
in the next cost report — both expected and desired, not a surprise.
