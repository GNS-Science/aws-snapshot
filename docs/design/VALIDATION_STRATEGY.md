# Backup Validation Strategy

## Overview

The backup system has two independent validation paths: one built into every
backup run (inventory-based), and one that bypasses the pipeline entirely
(direct listing). Together they provide defence in depth.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SOURCE BUCKETS                                  │
│        (toshi 7M, ths 4M, static 40M, weka 11 objects)            │
└──────────────┬──────────────────────────────────┬───────────────────┘
               │                                  │
               │  S3 Inventory (daily)             │  Direct S3 API
               │  Parquet snapshots                │  list_objects_v2
               ▼                                  │
┌──────────────────────────────┐                  │
│       ATHENA DIFF QUERY      │                  │
│  source vs backup inventory  │                  │
│  (smart ETag comparison)     │                  │
└──────────────┬───────────────┘                  │
               │                                  │
     ┌─────────┴──────────┐                       │
     │                    │                       │
     ▼                    ▼                       ▼
┌─────────┐    ┌──────────────┐         ┌─────────────────────┐
│ SKIPPED │    │ S3 BATCH     │         │ test integrity      │
│ (in     │    │ COPY JOB     │         │                     │
│  sync)  │    │ (manifest →  │         │ Lists BOTH buckets  │
│         │    │  CopyObject) │         │ directly via S3 API │
└─────────┘    └──────┬───────┘         │ Compares every key  │
                      │                 │ + ETag              │
                      ▼                 │                     │
               ┌──────────────┐         │ Independent of      │
               │ BACKUP       │         │ inventory + Athena  │
               │ BUCKETS      │◄────────┤                     │
               │              │         └─────────────────────┘
               └──────┬───────┘
                      │
                      ▼
               ┌──────────────┐
               │ test restore │
               │              │
               │ Random sample│
               │ (Athena RAND)│
               │ Copy to temp │
               │ Verify CRC64 │
               │ checksum     │
               └──────────────┘


 PATH A: Inventory Pipeline              PATH B: Direct Verification
 (runs daily, built into backups)        (independent audit)
```

## Path A: Inventory Pipeline (daily, automated)

Every scheduled backup run performs a full integrity check as a side effect:

1. **S3 Inventory** snapshots source and backup buckets daily (Parquet)
2. **Athena UNLOAD** diffs the two inventories (key + size + smart ETag)
3. Missing or changed objects → manifest → S3 Batch copy
4. No differences → "skipped" (everything is in sync)

This is the primary validation path. If it works correctly, the backup is
always complete and current (within inventory lag of ~24h).

**What could go wrong:**
- S3 Inventory delivers stale or incomplete data
- Athena query logic has a bug (e.g. the `is_latest = NULL` issue we found)
- S3 Batch copy fails silently on some objects
- Inventory lag means recently-written objects aren't visible yet

## Path B: Direct Verification (on-demand, independent)

`backup test integrity` bypasses the entire inventory + Athena pipeline.
It lists both source and backup buckets directly via `list_objects_v2` and
compares every key + ETag. This catches any failure mode in Path A.

**Limitation:** Full listing is only practical for small buckets. For
40M-object buckets it takes hours and loads millions of keys into memory.

**Strategy:** Use **weka as a canary**. Weka has 11 objects and the same
pipeline as the large sources (inventory mode, Athena UNLOAD, S3 Batch).
A monthly `test integrity --source weka` validates the entire pipeline
in seconds. If the pipeline is wrong for weka, it's wrong for everything.

## Path C: Restore Verification (on-demand)

`backup test restore` proves backed-up data is actually readable:

1. Samples N random objects from backup inventory (Athena `ORDER BY RAND()`)
2. Copies each to a temporary bucket
3. Compares CRC64NVME checksums (content-deterministic, not affected by
   multipart upload ETag differences)
4. Cleans up temp bucket

This catches a class of problems that neither Path A nor Path B detect:
corrupted data that has the right key/size/ETag but wrong content.

## Recommended testing cadence

| Test | Frequency | What it validates | Time |
|------|-----------|-------------------|------|
| Backup run (Path A) | **Daily** (automated) | Inventory diff → copy pipeline | ~30s |
| `test restore --source weka` | Weekly | Restore path + checksum integrity | ~15s |
| `test restore --source ths` | Weekly | Large-bucket restore + Athena sampling | ~15s |
| `test integrity --source weka` | Monthly | Independent pipeline audit (canary) | ~5s |
| `test restore --use-batch` | Monthly | S3 Batch restore IAM path | ~60s |
| `test full-drill` | Quarterly | Full DR exercise (not yet implemented) | TBD |

## Why two paths matter

If we only had Path A, a bug in inventory or Athena could silently leave
backups incomplete. We wouldn't know until a disaster required a restore.

If we only had Path B, we couldn't run it on production-scale buckets
(40M objects). We'd either skip validation or accept multi-hour runs.

The combination gives us:
- **Continuous automated validation** via the daily backup pipeline
- **Independent audit capability** via direct listing on the canary source
- **Restore confidence** via checksum-verified sample restores
