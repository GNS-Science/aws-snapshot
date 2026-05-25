# Storage Tiers

## S3 backup bucket lifecycle

Each object in a backup bucket ages through two cost tiers based on when it
was **last written** (initial copy or overwritten by a changed version):

```
Day 0–30     S3 Standard          $0.036/GB/month   immediate access
Day 30+      Glacier Instant      $0.007/GB/month   milliseconds retrieval
             (forever, no expiry)
```

For NSHM data (largely immutable scientific outputs), most objects are written
once and never updated. After 30 days they settle in Glacier Instant Retrieval
at $0.007/GB and stay there indefinitely. See
[ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
for the rationale (dropped Deep Archive and the 365-day expiration to
eliminate the silent annual re-copy and the unimplemented Deep Archive thaw
flow).

## Retrieval behaviour

| Tier | Retrieval time | Retrieval cost (NZD/GB) | When used |
|------|----------------|------------------------|-----------|
| S3 Standard | Immediate | — | Recent backups (< 30 days) |
| Glacier Instant | Milliseconds | $0.079 | Objects 30+ days old |

Retrieval cost is only incurred when an object is actually downloaded — normal
backup operations (listing, copying new objects) do not retrieve existing
backup objects.

## Implications for disaster recovery

A full restore of 9 TB from Glacier Instant Retrieval is bound by S3-to-S3
copy throughput — there is no archive thaw step. See
[Disaster Recovery Scenario](../design/disaster-recovery-scenario.md) for the
full RTO breakdown.

## Deleted source objects

The backup Lambda has no `s3:DeleteObject` permission. If a source object is
deleted, it remains in the backup bucket indefinitely. This is intentional
— it protects against accidental deletion propagating to backups. Intentional
removal of garbage from backup buckets is an out-of-band admin task; see the
manual-purge runbook (tracked under #23).

## DynamoDB export storage

DynamoDB PITR exports land in a dedicated export bucket
(`bb-{source}-dynamo-{region}-{acct}`) and follow the same lifecycle.
Export data is in DYNAMODB_JSON or Parquet format. At 18.3 GB per export,
monthly exports accumulate ~220 GB/year in Glacier Instant Retrieval
(~$1.55 NZD/month at steady state).

---

**Created:** 2026-03-17
**Updated:** 2026-05-25 (ADR-006)
