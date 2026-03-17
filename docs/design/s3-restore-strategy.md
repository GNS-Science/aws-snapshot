# S3 Restore Strategy: Batch Operations vs Direct Copy

## Decision

`backup restore run` will use **S3 Batch Operations** for all S3 restores,
regardless of bucket size.

`backup test restore` uses **direct `copy_object`** (fast, in-process) by default,
with a `--use-batch` flag to exercise the Batch path when explicitly requested.

---

## Rationale

### Why Batch for `restore run` (always)

| Property | Direct copy_object | S3 Batch Operations |
|----------|--------------------|---------------------|
| Runs server-side | No — dies if process is killed | Yes — job continues independently |
| Resumable | No — restart from scratch | Yes — job tracks per-object state |
| Per-object retry | No | Yes — configurable retry count |
| Completion report | No | Yes — success/failure manifest written to S3 |
| Progress tracking | In-process counter only | Poll job status; `restore status` works without keeping process alive |
| Memory pressure | Builds full target-objects index in-process | None — manifest is an S3 object |
| Suitable for 8 TB | No | Yes |
| Consistent code path | Requires size-based branching | Single path for all sizes |

The 8 TB ToshiBucket restore makes direct `copy_object` untenable — it would
run for hours in a blocking process with no resumability. Using Batch for all
restores avoids a two-code-path design where small buckets work differently
from large ones, and the operational model is consistent: submit → poll status.

### Why direct copy for `backup test restore` (default)

`test restore` copies a small sample (~10 objects) to a temporary bucket to
verify that the restore path is functional. For this smoke-test purpose:

- Direct copy completes in seconds vs minutes (Batch has per-job setup overhead)
- No IAM Batch role is required in CI/lightweight environments
- The temp-bucket-and-delete pattern is simpler without a Batch manifest lifecycle

The `--use-batch` flag allows the Batch path to be tested explicitly when
validating the full production restore flow or confirming IAM role permissions.

---

## Incremental restore (ETag-skip) is dropped for Batch

The current `restore_s3_bucket` implementation skips objects whose ETag already
matches in the target (incremental restore). S3 Batch does not natively support
per-object conditional logic.

For `restore run`, this is acceptable — a restore is a recovery operation where
a clean, known state is preferred over partial sync semantics. Always overwriting
simplifies the manifest to: list backup bucket → filter OPERATIONAL_PREFIXES →
submit.

Post-restore consistency can be confirmed with `backup test integrity`.

---

## Implementation outline

### `restore run` (Batch path)

1. List backup bucket, excluding `OPERATIONAL_PREFIXES`
2. Write a CSV manifest to `s3://<backup-bucket>/_manifests/restore-<timestamp>.csv`
3. Submit `create_job` with:
   - Operation: `S3PutObjectCopy`
   - Manifest: the CSV written above
   - Role ARN: `nzshm-backup-batch` (created by `scripts/create-batch-role.py`)
   - Report: written to `s3://<backup-bucket>/_batch-reports/`
4. Return the Job ID; print: `Batch job submitted: <id>. Check progress with restore status`

### `restore status` (extended)

Add S3 Batch job status alongside existing DynamoDB restore status:

```
[arkivalist] S3 restore jobs:
  ⋯ bb-toshi-s3-api-... → nzshm-toshi-api-data  Active (42% complete)  job: a1b2c3d4

[arkivalist] DynamoDB restore status:
  ✓ arkivalist-api-dev-events → arkivalist-api-dev-events-restored: ACTIVE
```

### `test restore --use-batch`

Runs the same Batch job submission against the sample manifest instead of using
`copy_object` directly. Useful for validating IAM role permissions and the Batch
code path without a full restore.

---

## IAM requirements

The `nzshm-backup-batch` role (see `scripts/create-batch-role.py`) must have:

- `s3:GetObject` on the backup bucket
- `s3:PutObject` on the restore target bucket
- Trust policy allowing `batchoperations.s3.amazonaws.com`

For cross-account restores (backup account → workload account), the target
bucket policy must allow `s3:PutObject` from the batch role ARN.

---

## Current status

- `backup restore run` — implemented with direct `copy_object` (suitable for current small buckets)
- `backup test restore` — implemented with direct `copy_object`; `--use-batch` flag pending
- S3 Batch path — **not yet implemented**; tracked as the next restore milestone

**Created:** 2026-03-18
