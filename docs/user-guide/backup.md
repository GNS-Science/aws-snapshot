# Backup Operations

## Pre-flight check

Before running a backup for the first time (or after config changes), run the pre-flight check:

```bash
backup check
backup check --source toshi
```

This validates IAM credentials, cross-account role assumption, source bucket read access,
backup bucket existence, S3 Batch role presence, and DynamoDB PITR status — without
enumerating objects. Completes in seconds. Fix any `FAIL` items before proceeding.

## Running a backup

```bash
# Backup a specific source
backup run --source toshi

# Backup all configured sources
backup run --source all

# Dry run — performs an access check only, no AWS writes
backup run --source toshi --dry-run

# Force a full sync (ignores ETag matching)
backup run --source toshi --full-sync
```

The `--dry-run` and `--verbose` flags are global options and can be placed before `run`:

```bash
backup --dry-run --verbose run --source toshi
```

## How incremental sync works

On each run the backup engine:

1. Lists all objects in the source bucket
2. For each object, compares its ETag against the corresponding backup object
3. Copies only objects that are **new** or have a **changed ETag**
4. Skips objects with matching ETags (already backed up)

```
Source bucket            Backup bucket
├── run-001.h5  ETag=A   ├── run-001.h5  ETag=A  → SKIP
├── run-002.h5  ETag=B   ├── run-002.h5  ETag=C  → COPY (changed)
└── run-003.h5           └── (missing)           → COPY (new)
```

Deleted source objects are **retained** in the backup bucket — the Lambda has no
`s3:DeleteObject` permission, so deletions never propagate. Objects expire via the
lifecycle policy at `max_age_days` (default 365).

## S3 Batch Operations (large buckets)

For sources with `use_s3_batch: true`, the backup engine submits an S3 Batch
Operations job instead of per-object `copy_object` calls. This is required for
buckets with millions of objects where per-object copy would exceed Lambda's
15-minute timeout. Production sources using S3 Batch: `toshi` (~8M objects),
`ths` (~4M objects), `static` (~40M objects).

The CLI submits the job and returns immediately:

```
Batch job submitted: job-12345 (8192 objects)
```

Monitor progress in the AWS console (S3 → Batch Operations) or check with `backup status`.

**Dry run for S3 Batch sources:** performs a single `list_objects_v2(MaxKeys=1)` access
check and returns immediately — it does not enumerate objects. `objects_in_manifest` is
reported as `-1` (not enumerated).

To enable S3 Batch for a source, set in your config:

```yaml
general:
  s3_batch_role_arn: arn:aws:iam::345678901234:role/nzshm-s3-batch-role

sources:
  toshi:
    use_s3_batch: true
```

Create the role with: `python scripts/create-backup-roles.py`

## DynamoDB exports

For sources with DynamoDB tables configured, each backup run initiates a
`ExportTableToPointInTime` export to the DynamoDB backup bucket.

```
Export initiated: ToshiAPI-FileTable → arn:aws:dynamodb:...:export/01234
```

Exports are asynchronous — the CLI submits and returns. The export typically
completes within 15–30 minutes for the ToshiAPI tables.

DynamoDB PITR (Point-in-Time Recovery) is always enabled separately — see
[Retention & Costs](../design/retention-strategy-and-costs.md#dynamodb-tables-toshiapi)
for the combined protection model.

## Backup bucket naming

Backup buckets are named deterministically:

- S3: `bb-{source}-s3-{label}-{region}-{account_id}`
- DynamoDB: `bb-{source}-dynamo-{region}-{account_id}`

For example, with `source=toshi`, `label=api`, `region=ap-southeast-2`,
`account=345678901234`:

```
bb-toshi-s3-api-ap-southeast-2-345678901234
bb-toshi-dynamo-ap-southeast-2-345678901234
```

## Checking backup status

```bash
backup status
backup status --source toshi
backup status --output json
```

Shows last run time, object counts copied/skipped, and any errors.
State is persisted to `_state/last-run.json` in the DynamoDB backup bucket.
