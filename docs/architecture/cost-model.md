# Cost Model

## Why not AWS Backup?

The custom solution replaces AWS Backup, which was costing approximately
**$1,700 NZD/month**. Understanding why requires comparing the storage pricing.

AWS Backup stores data in a **Backup Vault** — a managed tier with fixed pricing
that cannot be replaced with cheaper lifecycle tiers:

| Storage | NZD/GB/month |
|---------|-------------|
| AWS Backup vault (warm) | ~$0.079 |
| AWS Backup vault (cold, best case) | ~$0.008 |
| S3 Standard | $0.036 |
| S3 Glacier Instant | $0.007 |
| **S3 Glacier Deep Archive** | **$0.0017** |

For 9 TB of largely static, write-once BLOB data that ages into Deep Archive:

| Approach | 9 TB steady-state cost (NZD/month) |
|----------|------------------------------------|
| AWS Backup vault (cold, best case) | ~$720 |
| Custom solution (Deep Archive, aged) | ~$14–20 |
| **Ratio** | **~36–50×** |

### Can AWS Backup be reconfigured to close the gap?

No. The vault storage floor is fixed:

| Lever | Effect |
|-------|--------|
| Enable cold vault tier | Still ~$720/month for 9 TB — 36× more than Deep Archive |
| Reduce retention window | Loses protection; doesn't reach Deep Archive pricing |
| Disable S3 backup, keep DynamoDB only | DynamoDB PITR is free regardless — AWS Backup adds nothing |
| Cross-account vault | Same pricing in a different account |

### Where the custom solution's savings come from

1. **Lifecycle tiering** — write-once BLOBs age through Standard → Glacier Instant
   → Deep Archive. AWS Backup vault has no equivalent tier.
2. **Incremental ETag sync** — only copies changed objects. AWS Backup for S3
   creates full recovery points.
3. **DynamoDB PITR is free** — enabling it directly costs nothing. AWS Backup for
   DynamoDB charges $0.10–0.20/GB/month on top of free PITR.
4. **No vault overhead** — raw S3 storage is the floor; AWS Backup's vault is a
   managed abstraction with a substantial price premium baked in.

---

## Combined cost summary

Steady-state costs once the full corpus has aged into Deep Archive.
Production sources: `toshi` (8TB S3 + 18.3GB DynamoDB), `ths` (1TB), `static` (2.7TB), `weka` (80MB).

| Component | Method | NZD/month | NZD/year |
|-----------|--------|-----------|---------|
| ToshiAPI DynamoDB (18.3 GB) | PITR (free) + weekly export | ~$13 | ~$156 |
| ToshiAPI S3 (8 TB, aged) | S3 Batch + Deep Archive | ~$14 | ~$165 |
| THS S3 (1 TB, aged) | S3 Batch + Deep Archive | ~$2 | ~$20 |
| Static reports S3 (2.7 TB, aged) | S3 Batch + Deep Archive | ~$5 | ~$55 |
| Weka S3 (80 MB, aged) | Incremental + Deep Archive | <$1 | <$1 |
| S3 Batch operations (weekly, all sources) | — | ~$3 | ~$36 |
| Lambda + EventBridge | — | ~$10 | ~$120 |
| **Total (steady-state)** | | **~$47** | **~$552** |

> **Note:** During the initial sync period (first 3 months), 11.7 TB sits in Standard
> and Glacier Instant tiers — monthly cost is significantly higher while the corpus ages.
> See [Retention Strategy and Costs](../design/retention-strategy-and-costs.md)
> for the full lifecycle cost breakdown and churn-rate sensitivity table.
>
> **`static` source** (`nzshm22-static-reports`, ~40M objects / 2.7TB) was not included
> in the original cost model. At steady-state it adds ~$88 NZD/year (storage + S3 Batch).
> First-run S3 Batch cost for 40M objects: ~$63 NZD one-time.

### Active Experiment Mode uplift

During periods of active data churn (scientists running sensitivity analyses),
switching to daily DynamoDB exports increases costs:

| Cadence | DynamoDB export cost/year | Notes |
|---------|--------------------------|-------|
| Weekly (production default) | ~$156 NZD | Deployed cadence |
| Daily | ~$1,095 NZD | High-frequency sensitivity analysis |

S3 costs also rise during active churn — see the churn-rate table in
[Retention Strategy and Costs](../design/retention-strategy-and-costs.md).

---

## S3 Batch Operations cost impact

For large buckets, per-object `copy_object` calls would exceed Lambda's 15-minute timeout.
S3 Batch Operations submits an async job and exits. Production sources using S3 Batch:
`toshi` (~8M objects), `ths` (~4M objects), `static` (~40M objects).

| Scenario | `copy_object` | S3 Batch |
|----------|--------------|----------|
| First run — toshi (8M objects) | ~$63 NZD (+ failed/incomplete) | ~$13 NZD |
| First run — ths (4M objects) | timeout | ~$7 NZD |
| First run — static (40M objects) | timeout | ~$63 NZD |
| Weekly incremental (~few K changed) | ~$0.06 NZD | ~$0.39 NZD/run |

The $0.25 USD flat fee per Batch job dominates on small incremental runs (~$0.39 NZD/run).
For all three Batch sources running weekly: ~$61 NZD/year in job fees — immaterial in the
overall model.

Full details: [S3 Batch Operations](s3-batch-operations.md).

---

## Cross-account cost impact

The account isolation design (backup Lambda in a separate account from source data)
adds **zero additional cost**:

| Item | Cost |
|------|------|
| S3→S3 data transfer, same region, cross-account | Free |
| DynamoDB export to cross-account S3, same region | Free |
| `sts:AssumeRole` calls | Free |
| Additional AWS account (under Organizations) | Free |

Full details: [Account Isolation](../design/ACCOUNT_ISOLATION.md).

---

## Storage tier reference

| Tier | Days in backup bucket | NZD/GB/month | Retrieval time | Retrieval cost |
|------|----------------------|-------------|----------------|----------------|
| S3 Standard | 0–30 | $0.036 | Immediate | — |
| Glacier Instant | 31–90 | $0.007 | Milliseconds | $0.079/GB |
| Glacier Deep Archive | 91–365 | $0.0017 | 12–48 hours | $0.126/GB |
| Deleted | 365+ | — | — | — |

For a DR restore of the full 9 TB from Deep Archive:

| Item | Cost |
|------|------|
| Retrieval (9 TB × $0.126/GB) | ~$1,130 NZD (one-time, emergency only) |
| Data transfer out (if needed) | $0.114/GB beyond free tier |

Retrieval cost is incurred only during an actual disaster recovery — not during
normal incremental backup operations.

See also: [Storage Tiers](storage-tiers.md) for per-tier behaviour details.

---

**Created:** 2026-03-17
