# Athena Inventory Manifest Pipeline (THS-first)

## Purpose

Define the implementation path for inventory-based S3 Batch manifest generation
using Athena queries (not S3 Select), so large-source prep can move off live
bucket listing.

This document is the concrete follow-on design for ADR-002.

## Why this pivot

The initial inventory implementation attempted to read inventory Parquet with
`SelectObjectContent`. In production testing, this failed with
`MethodNotAllowed` on inventory Parquet objects.

Implication: S3 Select is not a portable dependency for this project.

Decision: use Athena as the query layer for inventory diffing.

## Scope

- Source canary: `ths`
- Keep current run contract: `backup run --source ths`
- Keep existing S3 Batch submission contract (`manifest CSV + ETag`)
- Keep existing backup bucket naming and data semantics

## Inventory layout assumptions

Inventory destination remains the control bucket:

- `s3://nzshm-backup-inventory-<backup-account-id>/inventory/<alias>/source/<source-bucket>/...`
- `s3://nzshm-backup-inventory-<backup-account-id>/inventory/<alias>/backup/<backup-bucket>/...`

Each inventory producer writes a `hive/` tree with `dt=...` partitions and
symlink files referencing Parquet in `data/`.

## Athena table strategy

Use one table per side (source, backup) for a source alias.

Key detail: inventory `hive/` uses symlink files, so table input format should
be `SymlinkTextInputFormat` with Parquet serde.

Example DDL template:

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS inv_ths_source (
  bucket string,
  key string,
  version_id string,
  is_latest boolean,
  is_delete_marker boolean,
  size bigint,
  last_modified_date timestamp,
  e_tag string,
  storage_class string,
  is_multipart_uploaded boolean,
  replication_status string,
  encryption_status string,
  object_lock_retain_until_date timestamp,
  object_lock_mode string,
  object_lock_legal_hold_status string,
  intelligent_tiering_access_tier string,
  bucket_key_status string,
  checksum_algorithm string,
  object_access_control_list string,
  object_owner string
)
PARTITIONED BY (dt string)
ROW FORMAT SERDE 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe'
STORED AS INPUTFORMAT 'org.apache.hadoop.hive.ql.io.SymlinkTextInputFormat'
OUTPUTFORMAT 'org.apache.hadoop.hive.ql.io.IgnoreKeyTextOutputFormat'
LOCATION 's3://<control-bucket>/inventory/ths/source/<source-bucket>/<source-bucket>/<inventory-id>/hive/';
```

Repeat for `inv_ths_backup` with backup inventory path.

Partition refresh options:

- `MSCK REPAIR TABLE` (simple, slower as partitions grow)
- or explicit `ALTER TABLE ADD PARTITION (dt='...') LOCATION '.../hive/dt=.../'`

Preferred for runtime: explicit partition add for latest `dt`.

## Diff query (v2 optimized)

Use latest available `dt` for source and backup independently.

```sql
WITH src AS (
  SELECT key, size, e_tag
  FROM inv_ths_source
  WHERE dt = :source_dt
    AND is_latest = true
    AND is_delete_marker = false
),
dst AS (
  SELECT key, size, e_tag
  FROM inv_ths_backup
  WHERE dt = :backup_dt
    AND is_latest = true
    AND is_delete_marker = false
    AND key NOT LIKE '_manifests/%'
    AND key NOT LIKE '_batch-reports/%'
    AND key NOT LIKE '_state/%'
)
SELECT s.key
FROM src s
LEFT JOIN dst d ON s.key = d.key
WHERE d.key IS NULL
   OR s.size <> d.size
   OR s.e_tag <> d.e_tag;
```

## Manifest materialization strategy

Athena returns key-only rows (no URL encoding). CLI/runtime then:

1. downloads Athena result CSV from query output location
2. URL-encodes keys in Python (`quote(key, safe='/')`)
3. writes final S3 Batch manifest to backup bucket `_manifests/...`
4. uses manifest ETag for `s3control:CreateJob`

Rationale:

- keeps URL-encoding behavior consistent with existing manifest writer
- avoids SQL function differences/edge cases for reserved characters

## Runtime flow integration

`batch_manifest_mode: inventory` path should execute:

1. resolve control bucket + inventory metadata paths
2. ensure Athena tables/partitions for source+backup latest snapshots
3. execute diff query and wait for completion
4. stream query result -> write S3 Batch manifest CSV
5. continue existing `prepare_only` or `CreateJob` flow

No change to command/scheduler interface.

## IAM and service dependencies

Backup runtime principal (Lambda/CodeBuild role) needs:

- `athena:StartQueryExecution`
- `athena:GetQueryExecution`
- `athena:GetQueryResults`
- `glue:GetDatabase`, `glue:GetTable`, `glue:CreateTable`, `glue:UpdateTable`
- `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on Athena query-results bucket/prefix
- existing inventory-read and manifest-write permissions

## Performance and cost expectations

- Inventory listing + Athena scan remains low-cent order per run when partition-pruned.
- Query runtime should be materially more stable than live listing path.
- Primary added overhead is orchestration plumbing (table/partition/query lifecycle).

## Delivery plan

1. Add Athena helper module (`athena_inventory.py`) with:
   - table ensure
   - latest partition discovery
   - diff query execution/wait
2. Integrate helper into `s3_batch.py` inventory mode.
3. Add integration tests with mocked Athena/Glue/S3 interactions.
4. Validate THS `--prepare-only` in production using CodeBuild runtime first.
5. If stable within limits, test Lambda path and then consider schedule cutover.

## Acceptance checks

- THS inventory mode runs without live source/backup listing.
- Manifest creation succeeds from Athena diff output.
- Reserved-character keys are encoded correctly in final manifest.
- Two consecutive THS scheduled runs succeed end-to-end.
