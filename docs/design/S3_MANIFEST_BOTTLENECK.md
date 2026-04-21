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

### Cost direction (CodeBuild vs Inventory)

Using current THS observations:

- Manifest-prep runtime in CodeBuild is about 50-60 minutes per run.
- Source/backup inventory scale is roughly 3.9M + 3.9M rows.

Approximate weekly per-run prep cost:

| Prep method | Typical per-run estimate | Notes |
|-------------|--------------------------|-------|
| CodeBuild (`BUILD_GENERAL1_MEDIUM`, ~58m) | ~NZD 0.8-2.0 | Runtime-priced; sensitive to compute tier and run length |
| Inventory + Athena (Parquet) | ~NZD 0.04-0.08 | Inventory listing + Athena scan; excludes S3 Batch job fee |

Interpretation:

- Inventory + Athena is expected to be an order of magnitude cheaper for manifest prep.
- S3 Batch job fees still apply in both approaches.
- Exact values depend on current AWS regional pricing and Athena scanned GB.

## Inventory design plan (THS-first)

This is the planned implementation path for inventory-based manifests.

### Scope

- Start with `ths` as the canary source.
- Keep existing backup buckets and S3 Batch submission contract.
- Preserve backup semantics (explicit cadence, no delete propagation).

### Architecture

1. **Inventory producers**
   - Enable daily S3 Inventory on:
     - source bucket (`ths-dataset-prod`)
     - backup bucket (`bb-ths-s3-dataset-prod-...`)
   - Output format: Parquet
   - Destination: dedicated control bucket/prefix (not backup data path)

2. **Inventory query layer**
   - Register inventory datasets in Glue Data Catalog.
   - Use Athena SQL to compute diff set:
     - join on key
     - include rows where backup key missing, or source ETag/size differs

3. **Manifest writer**
   - Export Athena result to CSV manifest format expected by S3 Batch:
     - `source-bucket,key`
     - URL-encoded key format (preserve `/`)
   - Write manifest to backup bucket `_manifests/`.

4. **Batch submission + monitoring**
   - Existing submission path calls `CreateJob` from manifest + ETag.
   - Existing status path tracks submitted job and terminal state.

5. **State tracking**
   - During transition: keep `_state/last-run.json` compatibility.
   - Preferred target: centralized run-state store (see compatibility contract).

### Delivery phases

- **Phase A (Inventory v1, fast path):**
  - Source inventory only (copy all listed source rows)
  - Purpose: prove workflow reliability

- **Phase B (Inventory v2, optimized):**
  - Source + backup inventory diff (key + ETag/size)
  - Purpose: restore copy minimization and reduce S3 Batch task counts

### Athena starter schema + diff query (v2)

Use external tables over Parquet inventories in a control bucket.

Example table shape (adjust paths/partition columns to actual inventory layout):

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS inv_ths_source (
  bucket string,
  key string,
  size bigint,
  etag string,
  is_latest boolean,
  is_delete_marker boolean,
  last_modified_date timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://<control-bucket>/inventory/ths-source/';

CREATE EXTERNAL TABLE IF NOT EXISTS inv_ths_backup (
  bucket string,
  key string,
  size bigint,
  etag string,
  is_latest boolean,
  is_delete_marker boolean,
  last_modified_date timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://<control-bucket>/inventory/ths-backup/';
```

Starter diff query (latest partitions) for manifest candidates:

```sql
WITH src AS (
  SELECT key, size, etag
  FROM inv_ths_source
  WHERE dt = '<source_dt>'
    AND is_latest = true
    AND is_delete_marker = false
),
dst AS (
  SELECT key, size, etag
  FROM inv_ths_backup
  WHERE dt = '<backup_dt>'
    AND is_latest = true
    AND is_delete_marker = false
)
SELECT s.key
FROM src s
LEFT JOIN dst d ON s.key = d.key
WHERE d.key IS NULL
   OR s.size <> d.size
   OR s.etag <> d.etag;
```

Manifest export shape for S3 Batch:

- Output CSV rows as: `ths-dataset-prod,<url_encoded_key>`
- Keep `/` unescaped in key paths
- Write to: `s3://bb-ths-.../_manifests/ths-dataset-prod-<run_id>.csv`

### Acceptance criteria (THS)

1. Scheduled run no longer performs full live source/destination listing in runtime path.
2. Manifest is generated from inventory snapshots and submitted successfully.
3. Two consecutive scheduled THS runs succeed end-to-end.
4. Per-run prep cost and runtime are lower/more stable than CodeBuild full-listing path.
5. No migration required for existing live backup buckets.

## Additional findings

- `toshi` currently has a separate IAM issue (`s3:PutBucketVersioning` denied on first-run bucket
  bootstrap for its S3 backup bucket).
- Temporary verification rule `nzshm-backup-ths-daily` was disabled to avoid repeated timeout loops
  while redesign work proceeds.

## Compatibility contract (interim -> future)

To keep live backup buckets compatible while moving from the interim CodeBuild approach to a
precomputed/inventory-based architecture, keep the following invariants stable.

### Must remain stable

1. **Backup bucket identity**
   - Keep existing naming/tagging conventions (`bb-{source}-...`, `ManagedBy=nzshm-backup`).
   - Do not require bucket renames or data migrations.

2. **Data-plane object semantics**
   - User data objects in backup buckets remain plain S3 objects under original keys.
   - Keep no-delete propagation and version-retention behavior unchanged.

3. **Batch submission contract**
   - Continue submitting S3 Batch jobs with CSV manifests + ETag.
   - Preserve description format so `backup status` can discover recent jobs reliably.

4. **Operational prefix behavior**
   - Keep operational metadata under reserved prefixes only.
   - Ensure restore/integrity tooling excludes all operational prefixes.

### Preferred direction: centralized system state

Current run state is stored per backup bucket at `_state/last-run.json`. For the next architecture,
prefer a centralized state store (e.g. DynamoDB table in backup account, optionally with S3 archive)
for workflow/run metadata.

Benefits:

- Avoids coupling status/workflow logic to any single backup bucket.
- Makes multi-step workflows (prepare, submit, monitor, finalize) easier to coordinate.
- Enables consistent querying/reporting across all sources and runs.
- Reduces risk when introducing new prep methods (CodeBuild, inventory, Step Functions).

Recommended state model (minimum):

- `run_id` (partition key)
- `source_alias`, `source_bucket`, `backup_bucket`
- `mode` (`inline`, `prepare_only`, `precomputed`, `inventory`)
- `phase` (`running`, `prepared`, `submitted`, `active`, `completed`, `failed`)
- `manifest_key`, `manifest_etag`, `objects_in_manifest`
- `batch_job_id`, `started_at`, `updated_at`, `completed_at`
- `error_code`, `error_message` (if failed)

Compatibility note:

- Keep writing `_state/last-run.json` during transition for backward compatibility with existing
  `backup status` behavior.
- Once centralized status is fully adopted, deprecate bucket-local state via a staged migration.

## Recommended next steps

1. Move large-source manifest generation out of Lambda runtime.
   - options: precomputed manifests, inventory-driven manifests, or Step Functions + worker.
2. Keep Lambda as orchestrator/submission path for batch jobs.
3. Fix `toshi` versioning permission gap before next S3 run.
4. Once architecture changes are in place, re-run matrix with the same criteria.
