# How It Works

## Two entry points, one engine

The backup logic lives in a shared Python library (`nzshm_backup`). There are
two ways to invoke it:

```
CLI mode (manual, on-demand):
  your terminal → poetry run backup run → boto3 → S3 / DynamoDB APIs

Lambda mode (scheduled, automated):
  EventBridge cron → Lambda invocation → same Python code → boto3 → S3 / DynamoDB APIs
```

`commands/run_backup.py` and `lambda_handler.py` are both thin entry points
that call the same underlying functions — `backup_source()` and
`export_dynamodb_table()`. No Lambda is required to run a backup manually.

## What each component does

| Component | File | Role |
|-----------|------|------|
| CLI entry point | `src/nzshm_backup/cli.py` | Registers all subcommand groups |
| Manual backup command | `src/nzshm_backup/commands/run_backup.py` | `backup run` — calls backup engine directly |
| Lambda entry point | `src/nzshm_backup/lambda_handler.py` | Handles EventBridge events, calls same engine |
| S3 backup engine | `src/nzshm_backup/s3_backup.py` | Incremental sync, bucket creation, lifecycle policy |
| DynamoDB backup engine | `src/nzshm_backup/dynamodb_backup.py` | PITR export initiation, export bucket setup |
| Schedule management | `src/nzshm_backup/commands/schedule.py` | Creates/enables/disables EventBridge rules |
| Config loader | `src/nzshm_backup/config/loader.py` | Reads `backup-config.yaml` (or `BACKUP_CONFIG_PATH`) |
| Config models | `src/nzshm_backup/config/models.py` | Pydantic schema for all config fields |

## What happens when `backup run --source toshi` executes

1. Load `backup-config.yaml` (or `BACKUP_CONFIG_PATH` env var)
2. Resolve account ID via `sts:GetCallerIdentity` (or use `123456789012` in dry-run)
3. **S3 loop** — for each bucket ARN in `sources.toshi.s3_buckets`:
   - Derive backup bucket name: `{bucket}-backup-{region}-{account_id}`
   - Create backup bucket if it doesn't exist (with lifecycle policy + delete-protection)
   - Incremental sync: list source objects, compare ETags, copy only changed/new objects
4. **DynamoDB loop** — for each table ARN in `sources.toshi.dynamodb_tables`:
   - Derive export bucket name: `nzshm-dynamo-backup-toshi-{region}-{account_id}`
   - Create export bucket if it doesn't exist (idempotent — no error if already exists)
   - Call `dynamodb:ExportTableToPointInTime` → returns an `ExportArn` immediately
   - Export runs asynchronously in AWS — it is **not** complete when the CLI exits

## Lambda is only needed for scheduled automation

| Capability | What provides it |
|-----------|-----------------|
| Manual backup on demand | `backup run` from your terminal |
| Scheduled weekly/daily backup | EventBridge rule → Lambda (requires `serverless deploy`) |
| Creating/managing schedules | `backup schedule add/remove/enable/disable` from your terminal |

`backup schedule add` creates EventBridge rules. Those rules need a Lambda
target to fire automatically. Until `lambda_arn` is set in `backup-config.yaml`
and a Lambda is deployed, the rules exist but have no target — running
`backup run` manually is the only way to trigger a backup.

## Backup bucket naming conventions

| Data type | Bucket name pattern |
|-----------|-------------------|
| S3 source backup | `{source-bucket-name}-backup-{region}-{account_id}` |
| DynamoDB export | `nzshm-dynamo-backup-{source-alias}-{region}-{account_id}` |

Including `{account_id}` in the name ensures global uniqueness across accounts
and prevents any cross-account confusion.

## S3 lifecycle tiers

All backup buckets (both S3 sync and DynamoDB export) get a three-tier
lifecycle policy applied at creation:

| Tier | Days | Storage class | Access time |
|------|------|--------------|-------------|
| Hot | 0–30 | S3 Standard | Immediate |
| Warm | 31–120 | S3 Glacier Instant (`GLACIER_IR`) | Milliseconds |
| Cold | 121–365 | S3 Glacier Deep Archive (`DEEP_ARCHIVE`) | 12–48 hours |
| Expire | 365+ | Deleted | — |

> **AWS constraint:** The Deep Archive transition must be at least 90 days after
> the Glacier IR transition. The code enforces this automatically:
> `deep_archive_days = max(warm_days, hot_days + 90)`.

## DynamoDB export is asynchronous

`export_table_to_point_in_time` returns immediately with an `ExportArn` and
status `INITIATED`. The actual export (writing Parquet/JSON files to S3) runs
in the background in AWS — typically minutes to hours depending on table size.

To check export progress:
```bash
aws dynamodb list-exports --region ap-southeast-2
aws dynamodb describe-export --export-arn <ExportArn>
```

PITR must be enabled on the source table before an export can be initiated.

## Dry-run mode

All operations support `--dry-run` (via the global flag `backup --dry-run`):
- S3 sync: lists what would be copied, skips all API write calls
- DynamoDB export: logs what would be exported, skips the export API call
- Bucket creation: skipped entirely
- Account ID resolution: uses `123456789012` placeholder (no `sts` call)

Dry-run output is prefixed with `[DRY RUN]`.

## Configuration resolution order

1. Explicit path passed to `load_config(path)`
2. `BACKUP_CONFIG_PATH` environment variable
3. `./backup-config.yaml` (default)

For Lambda runtime, config is passed as a JSON environment variable
(`BACKUP_CONFIG`) set during `serverless deploy`.
