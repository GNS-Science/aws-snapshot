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
2. Resolve backup account ID via `sts:GetCallerIdentity`; resolve source account ID from `source_account_id` in the source config (same as backup account for same-account sources, or the explicit cross-account ID)
3. **S3 loop** — for each bucket in `sources.toshi.s3_buckets`:
   - Derive backup bucket name: `bb-{source-key}-s3-{label}-{region}-{source-account-id}`
   - Create backup bucket if it doesn't exist (with lifecycle policy + delete-protection)
   - Incremental sync: list source objects, compare ETags, copy only changed/new objects
4. **DynamoDB loop** — for each table ARN in `sources.toshi.dynamodb_tables`:
   - Derive export bucket name: `bb-{source-key}-dynamo-{region}-{source-account-id}`
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

## Lambda timeout risk with large S3 buckets

> **Production concern:** The current S3 sync uses per-object `copy_object` API
> calls inside the Lambda. This does not scale to the production toshi bucket
> (~8 TB, ~8 million objects).

| Step | Cost at 8M objects |
|------|--------------------|
| `list_objects_v2` source | ~8,000 API calls, ~80s |
| `list_objects_v2` backup | ~8,000 API calls, ~80s |
| `copy_object` (first run / full sync) | ~8M calls, ~22 hours — **impossible** |
| `copy_object` (incremental, 0.1% changed) | ~8,000 calls, ~80s — feasible |

The Lambda timeout is 15 minutes (AWS maximum). Incremental runs after the
first sync are likely fine. A first-run or forced full sync will time out.

**Planned fix: S3 Batch Operations** — Lambda generates a diff manifest (CSV),
submits an `s3control:CreateJob`, and exits immediately. AWS runs the copy
asynchronously, following the same pattern as DynamoDB PITR exports. See
`docs/architecture/s3-batch-operations.md` for the implementation plan.

## How backup data accumulates

Each backup run is **incremental and additive** — no existing backup data is
ever overwritten or deleted by the tool:

| Scenario | Behaviour |
|----------|-----------|
| Object new in source | Copied to backup bucket |
| Object changed in source (ETag or size differs) | Copied, overwriting the backup copy |
| Object unchanged since last backup | Skipped |
| Object deleted from source | Remains in backup bucket (no delete propagation) |

This means the backup bucket is a **superset** of the source at any point in
time. Data deleted from the source is retained in the backup until the lifecycle
policy expires it (365 days by default).

## Delete protection

Every backup bucket gets a resource-based bucket policy with an explicit
`s3:DeleteObject` deny applied at creation time. This prevents accidental
deletion by any principal — including administrator roles — since explicit
denies in resource policies override identity-based allow policies.

### Manually deleting a backup bucket (e.g. to clean up a wrongly-named bucket)

Because the explicit deny overrides even `AdministratorAccess`, you must remove
the bucket policy before you can empty and delete the bucket:

```bash
# 1. Remove the no-delete policy
aws s3api delete-bucket-policy --bucket <bucket-name>

# 2. Empty the bucket
aws s3 rm s3://<bucket-name> --recursive

# 3. Delete the bucket
aws s3api delete-bucket --bucket <bucket-name>
```

> Only do this for buckets you are certain are safe to delete (e.g. wrongly-named
> buckets from a misconfigured run). Never remove the policy from a live backup bucket.

**Manual and scheduled backups share the same bucket.** A bucket created by
`backup run` from the CLI is recognised (via its `ManagedBy: nzshm-backup` tag)
and reused by the Lambda on subsequent scheduled runs, and vice versa. There is
no conflict between the two entry points.

A bucket that exists but was **not** created by this tool (no `ManagedBy:
nzshm-backup` tag) will cause an ABEND — this is a safety guard against
accidentally writing into an unrelated bucket.

## Backup bucket naming conventions

| Data type | Bucket name pattern | Example |
|-----------|-------------------|---------|
| S3 source backup | `bb-{source-key}-s3-{label}-{region}-{source-account-id}` | `bb-arkivalist-s3-deploy-ap-southeast-2-456789012345` |
| DynamoDB export | `bb-{source-key}-dynamo-{region}-{source-account-id}` | `bb-arkivalist-dynamo-ap-southeast-2-456789012345` |

`{source-key}` is the YAML key for the source (e.g. `arkivalist`, `toshi`).
`{label}` is a short human-readable string set per-bucket in config — no MD5 truncation.
`{source-account-id}` is the AWS account that **owns the data being backed up**,
not the account running the backup Lambda. This means:

- For same-account sources (toshi, ths) the two are identical.
- For cross-account sources (e.g. Arkivalist, account `456789012345`) the bucket
  name embeds the source account, making it immediately clear which system the
  backup belongs to.

**Examples:**

| Source | Backup bucket |
|--------|--------------|
| `arkivalist` s3 bucket labelled `deploy` (account `456789012345`) | `bb-arkivalist-s3-deploy-ap-southeast-2-456789012345` |
| DynamoDB tables for `arkivalist` source | `bb-arkivalist-dynamo-ap-southeast-2-456789012345` |
| DynamoDB tables for `toshi` source (account `345678901234`) | `bb-toshi-dynamo-ap-southeast-2-345678901234` |

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
