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

`--to-point-in-time` accepts:
- ISO 8601: `2026-03-15T09:00:00Z`
- Display format: `'2026-03-25 07:50 NZDT'` (paste directly from `backup events` output)
- Bare datetime: `'2026-03-25 09:00'` (assumed UTC)
- Known timezone abbreviations: `UTC`, `NZST`, `NZDT`, `AEST`, `AEDT`


```bash
# ISO 8601 (UTC)
backup restore run --source toshi \
    --tables ToshiAPI-FileTable \
    --to-point-in-time 2026-03-15T09:00:00Z

# Localised display format — paste directly from `backup events` output
backup restore run --source toshi \
    --tables ToshiAPI-FileTable \
    --to-point-in-time '2026-03-25 07:50 NZDT'

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
| 30+ days (forever) | Glacier Instant | Milliseconds | Small retrieval fee (~$0.079/GB) |

Per [ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
backup objects no longer transition to Deep Archive, so DR restores no longer
include a 12–48h thaw step — wall-clock time is bound by S3-to-S3 copy
throughput. A full DR restore of 9 TB from Glacier IR costs ~$709 NZD in
retrieval (one-time). See
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
