# Backup Validation Strategy

## Overview

The backup system has two independent validation paths: one built into every
backup run (inventory-based), and one that bypasses the pipeline entirely
(direct listing). Together they provide defence in depth.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SOURCE BUCKETS                                  │
│        (toshi 7M, ths 4M, static 40M, weka 11 objects).             │
└──────────────┬──────────────────────────────────┬───────────────────┘
               │                                  │
               │  S3 Inventory (daily)            │  Direct S3 API
               │  Parquet snapshots               │  list_objects_v2
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

## The ETag problem and how we solve it

### Historical timeline

| Year | S3 change | Impact on object comparison |
|------|-----------|---------------------------|
| 2006 | S3 launch | ETag = MD5 of content. Reliable for comparison. |
| 2010 | Multipart upload | ETag = MD5 of part-MD5s + `-N` suffix. **Broken** — same content, different ETag depending on chunk size. |
| 2022 | Additional checksums (opt-in) | CRC32, CRC32C, SHA1, SHA256 available per-request. Content-deterministic regardless of upload method. But only set if uploader explicitly requests it. |
| 2025 | Default integrity (boto3 1.36+) | SDK automatically computes CRC32/CRC64NVME on all uploads. No opt-in needed. S3 Batch copies get CRC64NVME automatically. |

**Current state of objects in production:**
- Source bucket objects: mostly pre-2022, no checksums
- Backup copies (made by S3 Batch 2026): have CRC64NVME
- Cross-account: `GetObjectAttributes` may not be permitted on source

**Concrete example — toshi (`nzshm22-toshi-api-prod`):** This bucket spans
2020–2026 and contains ~7M objects uploaded by different tools and SDK
versions over 6 years. Some objects are single-part (reliable ETags), some
are multipart (unreliable ETags), most have no checksums (pre-2022). The
backup copies (made by S3 Batch in 2026) all have CRC64NVME checksums but
may have different ETags from the source. No single comparison method works
for the entire bucket — which is exactly why the three-tier cascade exists.

Our three-tier verification handles all of these eras gracefully.

### Why ETags aren't reliable for comparison

S3 ETags are computed differently depending on how an object was uploaded:

- **Single-part upload** → ETag = MD5 of content (deterministic)
- **Multipart upload** → ETag = MD5 of concatenated part-MD5s + `-N` suffix
  (depends on part count and chunk boundaries, NOT just content)

When S3 Batch copies an object, it may use a different upload method than the
original. Two identical files can have different ETags:

```
Source:  "ecff333f8d530ab722377c9440c57342-2"   (multipart, 2 parts)
Backup:  "2ee248473e80f1310dcc6ae80005368f"      (single-part copy)
```

Same bytes, different ETags. A naive comparison flags this as a mismatch —
producing thousands of false positives (4,224 per THS run before we fixed it).

### Three-tier verification

Each comparison point in the system uses a cascade of increasingly
permissive checks:

```
  ETags match?
      │
      ├── YES → verified ✓
      │
      └── NO → try checksum comparison
                    │
                    ├── Both have CRC64/SHA256, values match → verified ✓
                    │
                    ├── Checksums differ → REAL MISMATCH ✗
                    │
                    └── Checksums unavailable → check ETag format
                              │
                              ├── Either ETag has '-N' suffix
                              │   (multipart) → skip (known false positive) ✓
                              │
                              └── Both single-part, differ → REAL MISMATCH ✗
```

### Where each tier is used

| Context | Tier 1: ETag | Tier 2: Checksum | Tier 3: Smart ETag |
|---------|-------------|-----------------|-------------------|
| **Athena diff** (Path A) | — | — | `strpos(e_tag, '-') = 0` in SQL |
| **test integrity** (Path B) | `!=` compare | `get_object_checksum` via `GetObjectAttributes` | `-` in ETag string |
| **test restore** (Path C) | fallback | `get_object_checksum` on source + copy | — |

### S3 checksums (CRC64NVME)

S3 now computes content-deterministic checksums alongside ETags. These are
**not** affected by upload method — same content always produces the same
checksum regardless of whether the upload was single-part or multipart.

- S3 Batch copies automatically get CRC64NVME checksums
- Available via `GetObjectAttributes` (not via `HeadObject` or S3 Inventory)
- `test restore` and `test integrity` use these when available
- Cross-account: requires `s3:GetObjectAttributes` permission on the reader
  role (not currently granted for source buckets — falls back to smart ETag)

### Why not just use checksums everywhere?

- **S3 Inventory doesn't include checksum values** — only `ChecksumAlgorithm`
  (which algorithm was used, not the value). So Athena diff queries can't
  compare checksums.
- **Cross-account access**: the backup reader role may not have
  `GetObjectAttributes` on source buckets. Checksum comparison falls back
  to smart ETag when source checksums are unavailable.
- **Pre-existing objects**: objects uploaded before checksum support was
  enabled may not have checksums at all.

The smart ETag comparison (skip multipart) handles these edge cases without
requiring infrastructure changes.

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
