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

Steady-state costs once the 9 TB corpus has fully aged into Deep Archive:

| Component | Method | NZD/month | NZD/year |
|-----------|--------|-----------|---------|
| ToshiAPI DynamoDB (18.3 GB) | PITR (free) + monthly export | ~$3 | ~$39 |
| ToshiAPI S3 (8 TB, aged) | Incremental sync + Deep Archive | ~$14 | ~$165 |
| THS S3 (1 TB, aged) | Incremental sync + Deep Archive | ~$2 | ~$20 |
| Lambda + EventBridge | — | ~$10 | ~$120 |
| **Total (steady-state)** | | **~$29** | **~$344** |

> **Note:** During the initial sync period (first 3 months), the 9 TB corpus sits
> in Standard and Glacier Instant tiers — monthly cost is closer to $588 NZD.
> See [Retention Strategy and Costs](../design/retention-strategy-and-costs.md)
> for the full lifecycle cost breakdown and churn-rate sensitivity table.

### Active Experiment Mode uplift

During periods of active data churn (scientists running sensitivity analyses),
switching to weekly or daily DynamoDB exports increases costs:

| Cadence | DynamoDB export cost/year | Notes |
|---------|--------------------------|-------|
| Monthly (default) | ~$39 NZD | Recommended steady-state |
| Weekly | ~$156 NZD | Active experiment periods |
| Daily | ~$1,095 NZD | High-frequency sensitivity analysis |

S3 costs also rise during active churn — see the churn-rate table in
[Retention Strategy and Costs](../design/retention-strategy-and-costs.md).

---

## S3 Batch Operations cost impact

For the ToshiBucket (~8 million objects), large initial syncs exceed Lambda's
15-minute timeout using per-object `copy_object` calls. S3 Batch Operations
submits an async job and exits:

| Scenario | `copy_object` | S3 Batch |
|----------|--------------|----------|
| First run (8M objects) | ~$63 NZD (+ failed/incomplete) | ~$13 NZD |
| Weekly incremental (~8K changed) | ~$0.06 NZD | ~$0.39 NZD |

The $0.25 USD flat fee per Batch job dominates on small incremental runs (~$13 NZD/year
for weekly toshi runs) — immaterial in the overall model. The first-run saving is
significant, and per-object `copy_object` simply cannot complete at 8M objects within
Lambda timeout.

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
