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

```bash
uv run python scripts/create-source-roles.py \
    --config backup-config.production.yaml --source toshi \
    --backup-account-id 737696831915 --profile nshm-admin

uv run python scripts/create-source-roles.py \
    --config backup-config.production.yaml --source ths \
    --backup-account-id 737696831915 --profile nshm-admin
```

Note: `--backup-account-id` is required because `general.lambda_arn` is not yet set (Lambda
not yet deployed). Script was patched to accept this flag alongside `--config/--source`.

Roles created/updated in `461564345538`:
- `arn:aws:iam::461564345538:role/nzshm-backup-reader` — reader + DynamoDB export permissions
- `arn:aws:iam::461564345538:role/nzshm-backup-restore` — PITR restore + tag management

Both role ARNs written back to `backup-config.production.yaml` for `toshi` and `ths`.

S3 bucket policies applied to `nzshm22-toshi-api-prod` allowing `nzshm-backup-batch-role`
read (backup direction) and write (restore direction).
Note: `nzshm22-toshi-api-prod-restore` does not exist yet — write policy will be applied at
restore time.

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

## Step 5 — Push config to SSM and run dry-run

```bash
backup config push
backup run --source toshi --dry-run
backup run --source ths --dry-run
```

_Status: pending_

---

## Step 6 — First live backup run

```bash
backup run --source toshi
backup run --source ths
backup status
```

_Status: pending_
