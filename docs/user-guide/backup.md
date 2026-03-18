# Backup Operations

## Running a backup

```bash
# Backup a specific source
backup run --source toshi

# Backup all configured sources
backup run --source all

# Dry run — shows what would be copied, no AWS writes
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
buckets with millions of objects (e.g. ToshiBucket with ~8M objects) where
per-object copy would exceed Lambda's 15-minute timeout.

The CLI submits the job and returns immediately:

```
Batch job submitted: job-12345 (8192 objects)
```

Monitor progress in the AWS console (S3 → Batch Operations) or wait for the
CloudWatch alarm to notify on completion.

To enable S3 Batch for a source, set in your config:

```yaml
general:
  s3_batch_role_arn: arn:aws:iam::595842668254:role/nzshm-s3-batch-role

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
`account=595842668254`:

```
bb-toshi-s3-api-ap-southeast-2-595842668254
bb-toshi-dynamo-ap-southeast-2-595842668254
```

## Checking backup status

```bash
backup status
backup status --source toshi
backup status --output json
```

Shows last run time, object counts copied/skipped, and any errors.
State is persisted to `_state/last-run.json` in the DynamoDB backup bucket.
