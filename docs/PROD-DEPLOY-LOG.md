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
