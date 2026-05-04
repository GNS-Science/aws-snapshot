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

### v1 — Lambda streaming (superseded)

The original approach streamed Athena result CSV through Lambda:

1. downloads Athena result CSV from query output location
2. URL-encodes keys in Python (`quote(key, safe='/')`)
3. writes final S3 Batch manifest to backup bucket `_manifests/...`
4. uses manifest ETag for `s3control:CreateJob`

**Problem discovered 2026-05-04:** For the `static` source (~40M objects, 4.7 GB
Athena result), Lambda OOM'd at 1024 MB. Even after switching to line-by-line
streaming (`iter_lines()`), the I/O rate (~1K rows/sec at 1 GB Lambda) meant
~8 hours to process — far beyond the 15-minute timeout. Scaling Lambda memory
does not resolve this; the approach is fundamentally I/O-bound.

### v2 — Athena UNLOAD (current)

Replace Lambda streaming with server-side CSV generation. Lambda only
orchestrates — no data flows through its memory.

#### Phase A: Athena UNLOAD

Wrap the diff query in `UNLOAD` to write CSV directly to S3:

```sql
UNLOAD (
  SELECT '{source_bucket}' AS bucket,
         REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
           key, '%', '%25'), ',', '%2C'), ' ', '%20'), '=', '%3D'),
           '(', '%28'), ')', '%29'), '"', '%22'), '#', '%23') AS key
  FROM ...
)
TO 's3://{control_bucket}/_manifests/unload/{source_alias}/{source_bucket}/{query_id}/'
WITH (format = 'TEXTFILE', field_delimiter = ',')
```

Key design decisions:

- **URL encoding in SQL:** Athena has no `url_encode()`, so a nested `REPLACE()`
  chain handles the known special characters. `%` is encoded first to avoid
  double-encoding. Commas are encoded to `%2C` to prevent TEXTFILE field splitting.
- **Validation:** A parameterized unit test confirms the REPLACE chain matches
  `quote(key, safe='/')` for all characters found in production data.
- **Output format:** `TEXTFILE` with `field_delimiter=','` produces bare CSV
  with no header and no quoting — exactly what S3 Batch expects.
- **Collision avoidance:** The UNLOAD prefix includes the Athena `query_id`
  (unique per execution), so concurrent Lambda invocations cannot collide.
- **Empty results:** UNLOAD with 0 matching rows writes no files to the prefix.

#### Phase B: S3 multipart-copy concatenation

S3 Batch `CreateJob` requires a single manifest file (one `ObjectArn` + `ETag`).
UNLOAD may produce multiple part files (~100-200 MB each). Concatenation uses
S3 server-side copy — no data through Lambda memory.

| UNLOAD output | Action |
|---------------|--------|
| 0 files | Return "skipped" (nothing to copy) |
| 1 file | `copy_object` to `backup_bucket/_manifests/{name}.csv` |
| N files, all parts >= 5 MB | Multipart upload with `UploadPartCopy` per file |
| N files, any non-last part < 5 MB | Download all + `PutObject` (safe: small parts = small total data) |

S3 multipart constraints: max 10,000 parts, each >= 5 MB (except last), max 5 TB.
A 40M-row manifest at ~130 bytes/row is ~4.7 GB with ~25-50 UNLOAD parts — well
within limits.

#### Row count

Run `SELECT COUNT(*)` as a separate Athena query (fast on Parquet) in parallel
with UNLOAD. Provides exact `objects_in_manifest` without streaming.

#### Cleanup

After successful concatenation, delete intermediate UNLOAD part files from the
control bucket `_manifests/unload/` prefix.

## Runtime flow integration

`batch_manifest_mode: inventory` path executes:

1. resolve control bucket + inventory metadata paths
2. ensure Athena tables/partitions for source+backup latest snapshots
3. execute UNLOAD query + COUNT(*) query (parallel)
4. wait for both queries to complete
5. concatenate UNLOAD output to single manifest via S3 multipart-copy
6. clean up intermediate UNLOAD files
7. continue existing `prepare_only` or `CreateJob` flow

No change to command/scheduler interface.

## IAM and service dependencies

Backup runtime principal (Lambda role) needs:

- **Athena:** `StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`,
  `ListDatabases`, `ListTables`, `GetDatabase`, `GetTableMetadata`
- **Glue Data Catalog:** full CRUD for databases, tables, and partitions —
  `Get*`, `Create*`, `Update*`, `Delete*`, `BatchCreatePartition`,
  `BatchDeletePartition` (Athena delegates all catalog ops to Glue)
- **S3:** `GetObject`, `PutObject`, `ListBucket`, `CopyObject` on control bucket
  (Athena results, UNLOAD output, inventory data) and backup bucket (manifest
  destination)
- Existing inventory-read and manifest-write permissions

## Operational caveats

### Inventory lag after first-ever backup (full-sync race)

When a source runs its first backup, the backup bucket goes from empty to
containing millions of objects. However, the **backup-side S3 Inventory** won't
reflect those new objects until the next daily inventory delivery (up to 24
hours later).

If a second backup run triggers before the backup inventory refreshes:
- The code still sees "no backup inventory partitions"
- Falls back to full-sync mode (source-only query)
- Re-submits all objects as a batch job — wasteful but **not destructive**
  (S3 Batch copy is idempotent; objects already present are overwritten
  identically)

**Operational rule:** after a first-ever backup, do not manually re-trigger
the same source until the next inventory cycle has completed (~24h). The
weekly schedule naturally avoids this. The log message
`"No backup inventory partitions for ... — treating as full sync"` is the
visible signal.

Future mitigation options:
- Guard against re-running if a recent batch job completed successfully and
  backup inventory hasn't refreshed since
- Write a "first backup completed" marker and skip full-sync re-submission
  until inventory catches up

### Inventory freshness determines effective RPO

The `effective` timestamp shown by `backup status` is
`min(source_inventory_dt, backup_inventory_dt)`. Objects written to the
source after this timestamp are not visible to the diff query until the
next inventory snapshot. With daily inventory, worst-case data lag is ~48h
(object written just after one snapshot, not picked up until the run after
the next snapshot).

## Performance and cost expectations

- Inventory listing + Athena scan remains low-cent order per run when partition-pruned.
- Query runtime should be materially more stable than live listing path.
- Primary added overhead is orchestration plumbing (table/partition/query lifecycle).
- With UNLOAD, manifest generation for 40M objects takes ~12 seconds (Athena
  server-side) + ~7 seconds (S3 multipart-copy concat) = ~20 seconds total.
  Lambda execution ~28 seconds at 432 MB. Cost per run: ~$0.01-0.05 (Athena
  scan + S3 operations).

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
