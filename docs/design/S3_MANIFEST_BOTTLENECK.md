# S3 Manifest Bottleneck

## Problem statement

Large S3 sources (`ths`, `toshi`, `static`) use S3 Batch, but the current backup path builds a
manifest inside Lambda before calling `s3control:CreateJob`.

That preparation phase currently does:

1. list all source objects
2. list all backup objects
3. diff by key/ETag/size
4. write `_manifests/*.csv`
5. only then submit the batch job

S3 Batch has no native "sync source to destination" diff operation. A manifest (or inventory-driven
object list) is required.

## Why this is risky at scale

The expensive work is in steps 1-3, before any batch job exists.

- If Lambda times out during this phase, `backup status` can show "no batch jobs found" even though
  invocations fired.
- This failure mode is independent of S3 Batch itself.

## Experiment matrix protocol

Goal: determine whether max Lambda resources make inline manifest prep reliable.

Protocol agreed with operator:

1. Set backup Lambda memory to max (`10240 MB`, timeout unchanged at `900s`).
2. Run sources in scan order: `ths`, `toshi`, `static`.
3. Record per-run:
   - pass/fail
   - duration
   - max memory used
   - whether a batch job was submitted
4. If all pass and each is `< 10m`, repeat 3x then binary-search down/up for minimum stable memory.
5. Abort early if max memory fails.

## Execution results (2026-04-16)

### Environment actions

- Updated function config for test window:
  - function: `nzshm-backup-service-prod-backup`
  - memory: `10240 MB`
  - timeout: `900s`
- Restored to baseline after experiment:
  - memory: `1024 MB`
  - timeout: `900s`

### Source scan

| Source | Result | Duration | Max memory | Notes |
|--------|--------|----------|------------|-------|
| `ths` | **FAIL** | `900000 ms` | `4126 MB` | Timed out while "Listing source objects in ths-dataset-prod"; no batch job submission |
| `toshi` | FAIL (different blocker) | `6315.40 ms` | `127 MB` | Immediate `AccessDenied` on `s3:PutBucketVersioning` for `bb-toshi-s3-api-prod-ap-southeast-2-461564345538`; DynamoDB exports still submitted |
| `static` | Not run | n/a | n/a | Matrix aborted per protocol once max-memory failure confirmed |

Representative THS report line:

```text
REPORT RequestId: 73dfbd1b-c3b3-4813-b29d-85b5adf8d88e  Duration: 900000.00 ms
Billed Duration: 901231 ms  Memory Size: 10240 MB  Max Memory Used: 4126 MB  Status: timeout
```

## Conclusion

The inline manifest-preparation design is not reliable for THS-scale data, even at max Lambda
memory. This is a hard fail at the first gate, so down-search is not meaningful.

Key implication: increasing memory alone does not resolve the architectural bottleneck.

## THS canary: CodeBuild compute matrix

To test whether more flexible compute (outside Lambda) could still hit a 15-minute
target, a THS-only canary matrix was run in CodeBuild with `--prepare-only`.

| Compute tier | Result | Manifest runtime | Manifest rows |
|--------------|--------|------------------|---------------|
| `BUILD_GENERAL1_2XLARGE` | Success | `3283s` (~54m43s) | `3,886,583` |
| `BUILD_GENERAL1_LARGE` | Success | `3585s` (~59m45s) | `3,886,583` |
| `BUILD_GENERAL1_MEDIUM` | Success | `3086s` (~51m26s) | `3,886,583` |
| `BUILD_GENERAL1_SMALL` | Failed | n/a | process exited `137` (likely OOM) |

Interpretation:

- Moving prep out of Lambda avoids the 15-minute hard timeout failure mode.
- However, for THS scale, full manifest prep still takes ~51-60 minutes on successful
  compute tiers, far above a 15-minute objective.
- Therefore, additional compute alone is insufficient if a sub-15-minute target is required.

## Inventory-based manifests

An inventory-based workflow reduces run-time listing pressure by using S3 Inventory snapshots
as the input dataset for manifest generation.

### How it works

1. Enable S3 Inventory on source buckets (and optionally backup buckets), with daily outputs to
   a control bucket.
2. A scheduled manifest-prep worker reads the latest inventory files and computes copy candidates.
3. Write an S3 Batch manifest to `_manifests/...` in the backup bucket.
4. Submit `s3control:CreateJob` using the prepared manifest.
5. Track status via `DescribeJob` and persist run state (`prepared`, `submitted`, `active`,
   terminal status).

### Why this helps

- Removes expensive live `ListObjectsV2` scans from backup-trigger time.
- Improves runtime predictability for very large buckets.
- Keeps explicit scheduled backup semantics (not continuous replication).
- Supports existing anti-poisoning controls (versioning + retention).

### Copy minimization options

- **V1 (simple):** source inventory only -> copy all objects listed in snapshot.
  - fastest to implement, higher copy volume.
- **V2 (optimized):** source + backup inventory diff by key/ETag/size.
  - lower copy volume, more processing complexity.

### Tradeoffs

- Inventory has freshness lag (typically daily), so it is not real-time.
- Manifest quality depends on inventory completeness/timing.
- Additional pipeline components are needed (inventory configuration + prep job).

### Fit for this project

Inventory-based manifests are compatible with backup-not-replication goals:
explicit cadence, no delete propagation, and recoverability-first behavior.

## Additional findings

- `toshi` currently has a separate IAM issue (`s3:PutBucketVersioning` denied on first-run bucket
  bootstrap for its S3 backup bucket).
- Temporary verification rule `nzshm-backup-ths-daily` was disabled to avoid repeated timeout loops
  while redesign work proceeds.

## Recommended next steps

1. Move large-source manifest generation out of Lambda runtime.
   - options: precomputed manifests, inventory-driven manifests, or Step Functions + worker.
2. Keep Lambda as orchestrator/submission path for batch jobs.
3. Fix `toshi` versioning permission gap before next S3 run.
4. Once architecture changes are in place, re-run matrix with the same criteria.
