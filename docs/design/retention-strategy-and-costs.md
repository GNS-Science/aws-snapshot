# Retention Strategy and Cost Analysis

## Overview

This document covers the recommended backup retention strategy and cost breakdown
for each data source. All costs in NZD (1 USD ≈ 1.57 NZD, Feb 2026).

---

## DynamoDB Tables (ToshiAPI)

### Data volumes

| Table | Size |
|-------|------|
| ToshiAPI-FileTable | 2.3 GB |
| ToshiAPI-ThingTable | 16 GB |
| **Total** | **18.3 GB** |

### Two independent mechanisms

**PITR (Point-in-Time Recovery)**
- Continuous backup stream maintained by AWS internally
- Recover to any **second** within the last 35 days
- Restores to a new DynamoDB table
- Cost: **free** (included in DynamoDB table pricing)
- Must be enabled per-table (`scripts/enable-pitr.py`)

**ExportTableToPointInTime**
- Materialises a full snapshot of the table at a chosen point to S3
- Self-contained — no dependency on previous exports
- Export cost: **$0.16 NZD/GB**
- 18.3 GB per export ≈ **$3 NZD per run**
- Provides durable, offline archival beyond the 35-day PITR window

### Frequency vs cost

| Frequency | Exports/year | Export fees/year | Storage (Deep Archive) | Total/year |
|-----------|-------------|-----------------|----------------------|------------|
| Weekly | 52 | $152 | ~$4 | **~$156 NZD** |
| Fortnightly | 26 | $76 | ~$4 | **~$80 NZD** |
| Monthly | 12 | $35 | ~$4 | **~$39 NZD** |
| Quarterly | 4 | $12 | ~$4 | **~$16 NZD** |
| PITR only | 0 | $0 | $0 | **$0** (35-day window only) |

Storage cost is negligible — 18.3 GB × 9 copies × $0.0017/GB ≈ $0.28/month regardless
of export frequency.

### Recommended strategy: PITR + monthly export

The optimal combination for full coverage at minimal cost:

- **PITR** → any-second precision recovery for the last 35 days (free)
- **Monthly export** → durable S3 snapshots at month boundaries (~$39 NZD/year)

Together these provide complete long-term retention with no recovery gaps:

```
|←—— monthly export ——→|←—— monthly export ——→|←—— monthly export ——→|
                    |←————————— 35-day PITR ————————————→|
                             ↑ always overlapping
```

Schedule exports every **28 days** (not calendar monthly) so the export window
always overlaps with the 35-day PITR window, eliminating any boundary gap.

### Active Experiment Mode

During periods of active sensitivity analysis, scientists need finer recovery
granularity to recover to a specific experiment run from days ago.

During these windows:
- Switch to **weekly exports** (~$3/week)
- Drop back to monthly when the experiment ends
- The CLI supports this: `backup schedule add --frequency daily` enables the
  second EventBridge rule for daily Lambda invocations

---

## S3 Buckets

### Data volumes

| Source | Bucket | Size |
|--------|--------|------|
| ToshiAPI | nzshm-toshi-api-data | 8 TB |
| THS | ths-dataset-prod | 1 TB |
| **Total** | | **9 TB** |

### How the backup relates to the source

#### Initial sync

On the first run, every object in the source bucket is copied to the backup bucket.
For 8 TB this takes time but costs nothing in transfer fees (same-region S3 copy
across accounts is free).

```
Source bucket                    Backup bucket
nzshm-toshi-api-data             bb-toshi-s3-api-ap-southeast-2-...
├── models/2024/run-001.h5  ──►  ├── models/2024/run-001.h5
├── models/2024/run-002.h5  ──►  ├── models/2024/run-002.h5
├── results/hazard-map.json ──►  ├── results/hazard-map.json
└── ...8 TB total...        ──►  └── ...8 TB copy...
```

#### Subsequent incremental runs

Each backup run compares every source object's **ETag** (an MD5-based checksum)
against the corresponding object in the backup bucket:

- **Same ETag** → already backed up, skip (no copy, no cost)
- **Different ETag** → object has changed, copy new version (overwrites backup copy)
- **Missing in backup** → new object, copy it

```
Week 2 run — only 3 objects changed out of millions:
Source bucket                    Backup bucket
├── models/2025/run-099.h5  ──►  ├── models/2025/run-099.h5  (NEW — copied)
├── results/hazard-map.json ──►  ├── results/hazard-map.json  (CHANGED — overwritten)
├── models/2024/run-001.h5       ├── models/2024/run-001.h5   (SAME — skipped)
└── ...                          └── ...
```

**No versioning** — the backup bucket holds exactly one copy of each object
(the most recent backed-up version). If a source object is updated three times
between backup runs, only the state at backup time is captured.

#### Deleted source objects are retained

The backup Lambda has **no** `s3:DeleteObject` permission. If a source object is
deleted, it remains in the backup bucket indefinitely until the lifecycle policy
expires it (365 days). This is intentional — it protects against accidental
deletion propagating to backups.

```
Source bucket                    Backup bucket
                                 ├── models/2024/run-001.h5  ← deleted from source,
├── models/2024/run-002.h5            still here until 365d expiry
└── ...
```

### Storage lifecycle

Each object in the backup bucket ages through three tiers based on when it was
**last written** (initial copy or overwritten by a changed version):

```
Day 0–30    S3 Standard          $0.036/GB/month   immediate access
Day 31–90   Glacier Instant      $0.007/GB/month   milliseconds retrieval
Day 91–365  Glacier Deep Archive $0.0017/GB/month  12–48 hours retrieval
Day 365+    Deleted              —
```

For NSHM data (largely immutable scientific outputs), most objects are written once
and never updated. After 3 months the bulk of the 8 TB corpus sits in Deep Archive
at $0.0017/GB — about **$14/month** for the entire ToshiBucket corpus.

### Cost breakdown

The plan models steady-state cost by accounting for the full 9 TB spread across
all tiers simultaneously (conservative — assumes data written at a constant rate
throughout the year):

```
S3 Standard    (0-30 days):   9 TB × 1 month  × $0.036  = $324 NZD/month
Glacier Instant (31-90 days): 9 TB × 2 months × $0.007  = $126 NZD/month
Deep Archive  (91-365 days):  9 TB × 9 months × $0.0017 = $138 NZD/month
                                                           ─────────────
                                              Annual:     $7,056 NZD
                                              Monthly avg:  ~$588 NZD
```

For data that is largely **static** (most NSHM outputs are write-once), the
real steady-state cost is lower — the majority of 9 TB drops to Deep Archive
quickly after the initial sync, making the actual monthly cost closer to
**$14–$20 NZD/month** once the corpus has aged through.

### Backup poisoning (mutation propagation)

The incremental sync uses ETag comparison to detect changed objects. This means
a mutated source object (data corruption, human error, or attacker) is actively
copied to the backup on the next run, **overwriting the good backup copy**:

```
Source object mutated → ETag changes → backup detects difference
→ copies mutated version → overwrites good backup copy → good copy gone
```

For write-once BLOBs this is unlikely in normal operation, but it is the exact
scenario a malicious actor would exploit — corrupt the source, wait for the next
backup run, then delete both.

**DynamoDB — already protected by PITR.** AWS maintains a continuous change stream
independently of backup runs. Restore to any second before the corruption, as long
as it is detected within 35 days. Monthly exports add a secondary safety net but
can themselves capture corrupt state if run after the mutation.

**S3 — currently unprotected.** Fix: enable versioning on the backup bucket with a
lifecycle rule to expire non-current (superseded) versions after a chosen retention
period.

```
Week 1 sync:  models/run-001.h5  v1  ← good copy
Week 3 sync:  models/run-001.h5  v2  ← corrupted copy propagated from source
              models/run-001.h5  v1  ← still here, recoverable
```

Recovery: list object versions, restore the last known-good version.

**Choosing the non-current version retention period:**

| Retention | Protection window | Guidance |
|-----------|-------------------|----------|
| 30 days | Detect within a month | Tight — only safe if active monitoring |
| 90 days | Detect within a quarter | Aligns with quarterly DR drill |
| 365 days | Matches overall backup max age | Maximum safety, minimal extra cost |
| Indefinite | Full history | Unbounded cost growth |

For NSHM, 90 days aligns with "caught by the quarterly DR drill at worst." 365 days
is also defensible — for write-once data the only extra versions created are the
corrupted ones, so the cost difference is negligible.

**Cost for write-once BLOBs:** near zero in practice. Only mutated objects generate
an extra version; all unmodified objects have exactly one version as before.

| Corruption extent | Extra storage (90-day retention) |
|-------------------|----------------------------------|
| 10 GB corrupted | ~$0.17 NZD |
| 100 GB corrupted | ~$1.70 NZD |
| 1 TB corrupted | ~$17 NZD |

**Not currently implemented** — enabling versioning on backup buckets is a
one-line infrastructure change plus a lifecycle rule. Listed as a future
improvement in the implementation gaps.

### Versioning

S3 versioning is **not enabled** on backup buckets (current state). For
write-once BLOBs under normal operation this is sufficient — no object is ever
overwritten so no extra versions would be created. Versioning should be enabled
as a future improvement to protect against backup poisoning (see above).

### Source bucket versioning (THS)

For the THS source bucket (`ths-dataset-prod`), enabling S3 versioning provides
deletion protection that backups alone cannot replicate:

**How deletion works with versioning enabled:**

When an object is deleted from a versioned bucket, S3 inserts a **delete marker**
rather than removing the data. The object is invisible to normal `ListObjects`
calls but all previous versions remain. Recovery is instant — delete the marker
and the object reappears with no data transfer or restore wait.

| Scenario | Without versioning | With versioning |
|----------|--------------------|-----------------|
| Accidental delete (single object) | Gone — recover from backup bucket (12–48h if in Deep Archive) | Instant — delete the marker |
| Bulk accidental delete | Gone — full restore from backup | Instant — bulk-delete the markers |
| Malicious delete (attacker with S3 access) | Gone — recover from backup | Gone — attacker can delete markers too (unless MFA Delete enabled) |

**Cost implications for write-once BLOBs:**

Since THS objects are never overwritten, versioning creates at most one version per
object plus a delete marker if deleted. Delete markers are negligible in size
(< 1 KB each). For a 1 TB corpus of write-once BLOBs, enabling versioning adds
effectively **zero storage cost**.

**Recommendation:** Enable S3 versioning on `ths-dataset-prod`. The backup bucket
(`bb-ths-s3-dataset-ap-southeast-2-595842668254`) does not need versioning — it
already retains deleted objects for 365 days via the no-delete Lambda policy.

**MFA Delete caveat:** Versioning alone does not protect against a malicious actor
with full S3 permissions — they can delete object versions and markers. MFA Delete
requires a second factor to permanently delete versions, but adds operational
complexity (every delete must be authenticated with MFA). Consider enabling MFA
Delete if the source account threat model includes insider threat or full credential
compromise.

### Churn rate matters

| Churn (new/changed data per week) | Monthly backup cost (steady state) |
|-----------------------------------|-------------------------------------|
| ~0 GB (fully static after initial sync) | ~$16 NZD (Deep Archive only) |
| ~10 GB/week | ~$22 NZD |
| ~100 GB/week | ~$75 NZD |
| ~1 TB/week (active experiment) | ~$450 NZD |

During **Active Experiment Mode** (daily backups, high data churn), costs rise
because more data stays in the Standard and Glacier Instant tiers. Switch back
to weekly cadence once experiments complete to let data age into Deep Archive.

---

## Combined Cost Summary

| Source | Method | Annual cost (NZD) |
|--------|--------|-------------------|
| ToshiAPI DynamoDB (18.3 GB) | PITR + monthly export | ~$39 |
| ToshiAPI S3 (8 TB) | Incremental sync + lifecycle | *(TBD)* |
| THS S3 (1 TB) | Incremental sync + lifecycle | *(TBD)* |
| Lambda + infrastructure | — | ~$120 |
| **Total** | | **~$618/month (see backup-solution-plan.md)** |

---

## Reference: Pricing

| Service | Unit | NZD |
|---------|------|-----|
| S3 Standard | GB-month | $0.036 |
| S3 Glacier Instant | GB-month | $0.007 |
| S3 Glacier Deep Archive | GB-month | $0.0017 |
| DynamoDB Export | GB | $0.16 |
| Glacier Retrieval (Instant) | GB | $0.05 |
| Glacier Retrieval (Deep Archive) | GB | $0.08 |

**Created:** 2026-03-16
