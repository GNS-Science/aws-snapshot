# Restore Operations

## Overview

The restore target is always a **new** resource — a `{source}-restore` bucket or a
`{table-name}-restore` table. This prevents accidental overwrites of live data.

For the DR naming convention and rationale, see
[S3 Restore Strategy](../design/s3-restore-strategy.md) and
[Disaster Recovery](../design/disaster-recovery-scenario.md).

## S3 restore

```bash
# Restore a full bucket
backup restore run --source toshi --buckets nzshm-toshi-api-data

# Restore a specific prefix
backup restore run --source toshi \
    --buckets nzshm-toshi-api-data \
    --prefix models/2026/

# Restore to the original bucket name (non-default)
backup restore run --source toshi --buckets nzshm-toshi-api-data --original
```

The default restore destination is `{backup-bucket-name}-restore`. Using `--original`
restores directly to the source bucket name — only appropriate when the source bucket
has been fully deleted or you are performing a production cutover.

For large buckets, restore uses S3 Batch Operations when `s3_batch_role_arn` is
configured. For small buckets or explicit testing, direct `copy_object` is used.

## DynamoDB restore

DynamoDB restores are **asynchronous** — the CLI submits the restore job and returns.

```bash
# Restore to a point in time (ISO 8601, UTC)
backup restore run --source toshi \
    --tables ToshiAPI-FileTable \
    --to-point-in-time 2026-03-15T09:00:00Z

# Restore without re-enabling PITR on the restored table
backup restore run --source toshi --tables ToshiAPI-FileTable --no-pitr
```

The restore target table name is `{original-table-name}-restore`.

By default, PITR is automatically re-enabled on the restored table by the
`pitr-watcher` Lambda (polls SSM for pending re-enables). Use `--no-pitr`
for short-lived test restores to avoid unnecessary PITR costs.

## Checking restore status

```bash
backup restore status --source toshi
backup restore status --source toshi --tables ToshiAPI-FileTable
```

Shows the current status of in-progress restores (DynamoDB table restore states,
S3 Batch job progress).

## Storage tier and retrieval time

| Backup age | Storage tier | Retrieval time | Notes |
|------------|-------------|----------------|-------|
| 0–30 days | S3 Standard | Immediate | No retrieval cost |
| 31–120 days | Glacier Instant | Milliseconds | Small retrieval fee |
| 121–365 days | Glacier Deep Archive | 12–48 hours | Higher retrieval fee; DR use only |

For a full DR restore of 9 TB from Deep Archive, retrieval costs ~$1,130 NZD
(one-time). Plan for 12–48 hour restore time. See
[Cost Model](../architecture/cost-model.md#storage-tier-reference) for pricing.

## DR drill checklist

1. Confirm backup age and storage tier with `backup status`
2. Run `backup test integrity --source toshi` to verify ETag parity
3. Submit restore: `backup restore run --source toshi --buckets ...`
4. Monitor: `backup restore status --source toshi`
5. Validate: spot-check objects in the restore bucket
6. For DynamoDB: verify item counts and sample records match expected values
7. Document results and restore time for compliance records

## Tags on restored tables

Restored DynamoDB tables receive informational tags:

| Tag | Value |
|-----|-------|
| `RestoredBy` | `nzshm-backup-cli` |
| `RestoredFrom` | original table ARN |
| `RestoredAt` | ISO 8601 timestamp |
