# Storage Tiers

## S3 backup bucket lifecycle

Each object in a backup bucket ages through three cost tiers based on when it
was **last written** (initial copy or overwritten by a changed version):

```
Day 0–30    S3 Standard          $0.036/GB/month   immediate access
Day 31–90   Glacier Instant      $0.007/GB/month   milliseconds retrieval
Day 91–365  Glacier Deep Archive $0.0017/GB/month  12–48 hours retrieval
Day 365+    Deleted              —
```

For NSHM data (largely immutable scientific outputs), most objects are written
once and never updated. After 3 months the bulk of the 9 TB corpus sits in
Deep Archive at $0.0017/GB.

## Retrieval behaviour

| Tier | Retrieval time | Retrieval cost (NZD/GB) | When used |
|------|----------------|------------------------|-----------|
| S3 Standard | Immediate | — | Recent backups (< 30 days) |
| Glacier Instant | Milliseconds | $0.079 | Objects 31–90 days old |
| Glacier Deep Archive | 12–48 hours | $0.126 | Objects 91–365 days old |

Retrieval cost is only incurred when an object is actually downloaded — normal
backup operations (listing, copying new objects) do not retrieve existing
backup objects.

## Implications for disaster recovery

A full restore of 9 TB from Deep Archive takes 12–48 hours for the retrieval
initiation phase, plus the S3-to-S3 copy time. See
[Disaster Recovery Scenario](../design/disaster-recovery-scenario.md) for the
full RTO breakdown.

For objects still in Glacier Instant (31–90 days old), retrieval is immediate
— only the copy time applies.

## Deleted source objects

The backup Lambda has no `s3:DeleteObject` permission. If a source object is
deleted, it remains in the backup bucket and continues aging through the
lifecycle tiers until Day 365, when the expiry rule removes it. This is
intentional — it protects against accidental deletion propagating to backups.

## DynamoDB export storage

DynamoDB PITR exports land in a dedicated export bucket
(`bb-{source}-dynamo-{region}-{acct}`) and follow the same lifecycle tiers.
Export data is in DYNAMODB_JSON or Parquet format. At 18.3 GB per export,
monthly exports accumulate ~220 GB/year — mostly in Deep Archive at negligible
cost (~$0.37 NZD/month).

---

**Created:** 2026-03-17
