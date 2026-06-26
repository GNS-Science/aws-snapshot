# Backup Forensics: Investigating Suspected Mutations

## Scenario

You suspect that some S3 objects were mutated (data corruption, human error, or
attacker) during a specific time window, and that those mutations may have been
propagated into the backup bucket by the incremental sync.

---

## How mutations propagate into backup

The incremental sync copies any object whose ETag differs between source and
backup. A mutated source object therefore overwrites the good backup copy on the
next backup run:

```
Source object mutated  →  ETag changes  →  backup detects difference
→  copies mutated version  →  overwrites good backup copy
```

If S3 versioning is enabled on the backup bucket, the previous (good) copy is
retained as a non-current version rather than destroyed. This is the foundation
for forensic investigation and recovery.

---

## Metadata available from S3 versioning

Each object version in the backup bucket carries:

| Field | Description |
|-------|-------------|
| `VersionId` | Unique identifier for this version |
| `LastModified` | When this version was written to the **backup** bucket |
| `ETag` | MD5 of the content — changes if content was mutated |
| `Size` | Object size in bytes |
| `IsLatest` | Whether this is the current version |

**Important:** `LastModified` on a backup version is the timestamp of the backup
run that copied it — not the timestamp of the original mutation in the source
bucket. The source mutation could have occurred any time between the previous
backup run and the run that captured it.

```
Source mutated:   March 3  (unknown until investigated)
Backup run:       March 7  ← LastModified you see in list-object-versions
Previous run:     March 1  ← good copy; mutation happened sometime March 1–7
```

---

## Finding mutated objects in a time window

### Small scope (known key prefix)

For targeted investigation of a specific prefix:

```bash
aws s3api list-object-versions \
    --bucket bb-toshi-s3-api-ap-southeast-2-345678901234 \
    --prefix models/2026/ \
    --query "Versions[?LastModified >= '2026-03-01' && LastModified <= '2026-03-15']" \
    --output table
```

Any object with a version `LastModified` in the suspect window had a new version
written during that period — i.e., the backup Lambda detected an ETag change
and overwrote the previous copy.

To confirm the content actually changed (not a spurious re-copy), compare ETags
between the current and prior version:

```bash
aws s3api list-object-versions \
    --bucket bb-toshi-s3-api-ap-southeast-2-345678901234 \
    --prefix models/2026/run-099.h5 \
    --query "Versions[*].{VersionId:VersionId,Modified:LastModified,ETag:ETag,Latest:IsLatest}"
```

### Full bucket scan (9 TB)

`list-object-versions` is impractical for a full bucket scan at millions of
objects. Use **S3 Inventory** instead:

1. Enable daily or weekly S3 Inventory on the backup bucket, outputting to a
   separate audit bucket in Parquet or CSV format — includes all versions and
   their metadata
2. Query with **Athena** — fast even at billions of objects:

```sql
SELECT key, version_id, last_modified, etag, size, is_latest
FROM s3_inventory.backup_versions
WHERE last_modified BETWEEN '2026-03-01' AND '2026-03-15'
  AND is_latest = 'false'
ORDER BY key, last_modified DESC;
```

Objects appearing in this result had their current version superseded during
the window — candidates for mutation.

---

## Correlating backup run timestamps

To determine the exact backup run that propagated the mutation, check CloudWatch
logs for the Lambda function:

```bash
aws logs filter-log-events \
    --log-group-name /aws/lambda/nzshm-backup-dev \
    --start-time <epoch-ms> \
    --end-time <epoch-ms> \
    --filter-pattern "COPY"
```

Each backup run logs the objects it copies. Cross-referencing the CloudWatch
timestamps with the `LastModified` values from `list-object-versions` confirms
which run introduced the mutation into the backup.

---

## Recovery

Once suspect objects are identified, restore from the prior version:

```bash
# Copy the known-good version back to the current version
aws s3api copy-object \
    --bucket bb-toshi-s3-api-ap-southeast-2-345678901234 \
    --copy-source "bb-toshi-s3-api-ap-southeast-2-345678901234/models/2026/run-099.h5?versionId=<good-version-id>" \
    --key models/2026/run-099.h5
```

For bulk recovery, S3 Batch Operations can restore a list of objects to their
prior versions using a manifest generated from the Athena query above.

After recovery, re-sync the restored objects back to the source bucket — the
backup is the authoritative copy if the source was compromised.

---

## Implementation gaps

| Gap | Impact | Notes |
|-----|--------|-------|
| Versioning not enabled on backup buckets | No prior versions retained — forensic recovery impossible | One-line infrastructure change + lifecycle rule |
| S3 Inventory not configured | Full-bucket mutation scan requires slow `list-object-versions` pagination | Enable on backup buckets, output to audit bucket |
| No `backup forensics` subcommand | Manual AWS CLI required for all steps above | Future Phase 4/5 work |
| No automated integrity check | Mutations go undetected until a DR drill or user report | `backup test integrity` not implemented |

---

**Created:** 2026-03-17
