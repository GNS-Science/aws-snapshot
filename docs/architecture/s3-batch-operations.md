# S3 Batch Operations — Architecture Plan

## Problem

The current `sync_bucket()` implementation uses per-object `copy_object` API calls
inside the Lambda. At ~8 million objects (toshi production bucket) this is not viable:

| Step | Cost at 8M objects |
|------|--------------------|
| `list_objects_v2` source | ~8,000 API calls, ~80s |
| `list_objects_v2` backup | ~8,000 API calls, ~80s |
| `copy_object` — first run | ~8M calls, ~22 hours — **impossible** |
| `copy_object` — incremental (0.1% changed) | ~8,000 calls, ~80s — feasible |

Lambda's maximum timeout is 15 minutes. First-run and forced full-sync will always
exceed this.

## Solution: S3 Batch Operations

Follow the same async pattern already used for DynamoDB exports:

```
Current (sync, broken at scale):
  Lambda → copy_object × 8M → exits after 15min timeout (incomplete)

Target (async, same pattern as DynamoDB):
  Lambda → list source + backup → build diff manifest → s3control.create_job → exits
                                                  ↓
                                   AWS runs batch job asynchronously (hours)
```

Lambda submits a **manifest CSV** listing only new/changed objects, then calls
`s3control:CreateJob` and exits immediately. AWS handles the copy.

---

## New Module: `s3_batch.py`

Sibling to `s3_backup.py`. Key components:

**`BatchJobResult` dataclass** — mirrors `ExportResult` from `dynamodb_backup.py`:
```python
@dataclass
class BatchJobResult:
    source_bucket: str
    dest_bucket: str
    job_id: str | None
    manifest_key: str
    objects_in_manifest: int
    status: Literal["SUBMITTED", "SKIPPED", "FAILED"]
    errors: list[dict]
    dry_run: bool
```

**`build_manifest_csv(source_objects, dest_objects, source_bucket) -> Iterator[str]`**
Pure diff logic (same ETag/size comparison as `sync_bucket`), yields CSV rows as a
stream rather than building the full string in memory (see Memory Risk below).

**`write_manifest_to_s3(s3_client, rows, backup_bucket, manifest_key) -> str`**
Streams rows to `s3://{backup_bucket}/_manifests/{manifest_key}` via multipart upload.
Returns the S3-assigned ETag (required by `create_job` for manifest integrity).

**`batch_backup_source(session, source_bucket, backup_bucket, batch_role_arn, dry_run, full_sync) -> BatchJobResult`**
Top-level function. Flow:
1. `ensure_backup_bucket_ready()` (unchanged from `s3_backup.py`)
2. List source objects via paginator
3. List backup objects via paginator
4. Build and stream manifest to S3 under `_manifests/`
5. If manifest is empty → return `status="SKIPPED"`
6. Call `s3control.create_job()` → return `status="SUBMITTED"` with job ID

---

## Config Changes

**`GeneralConfig`** — add:
```yaml
s3_batch_role_arn: null  # ARN of IAM role S3 Batch assumes; required when any source has use_s3_batch: true
```

**`SourceConfig`** — add:
```yaml
use_s3_batch: false  # set true for large buckets (toshi)
```

**Cross-field validation** on `ConfigModel`:
```python
@model_validator(mode="after")
def validate_batch_config(self):
    if any(s.use_s3_batch for s in self.sources.values()):
        if not self.general.s3_batch_role_arn:
            raise ValueError("general.s3_batch_role_arn required when use_s3_batch: true")
    return self
```

**`backup-config.example.yaml`:**
```yaml
general:
  s3_batch_role_arn: null  # arn:aws:iam::ACCOUNT:role/nzshm-backup-batch-role

sources:
  toshi:
    use_s3_batch: true   # ~8M objects — must use Batch Operations
  ths:
    use_s3_batch: false  # ~1TB, incremental runs are within Lambda timeout
```

---

## IAM Changes

### Lambda role (`serverless.yml`) — add:
```yaml
- Effect: Allow
  Action:
    - s3control:CreateJob
    - s3control:DescribeJob
    - s3control:ListJobs
  Resource: "*"   # s3control does not support resource-level ARN scoping
- Effect: Allow
  Action:
    - iam:PassRole
  Resource:
    - "arn:aws:iam::ACCOUNT_ID:role/nzshm-backup-batch-role"
```

### Batch role (created once, outside serverless.yml)

Trust policy:
```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "batchoperations.s3.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Permission policy:
- `s3:GetObject`, `s3:GetObjectTagging` on source bucket
- `s3:PutObject`, `s3:PutObjectTagging`, `s3:GetBucketLocation` on backup bucket
- `s3:GetObject` on `_manifests/` prefix in backup bucket (to read the manifest)
- `s3:PutObject` on `_batch-reports/` prefix in backup bucket (job completion reports)

A helper script `scripts/create-batch-role.py` should create this role.
Add `s3_batch_role_arn` to config after creating it.

---

## Call Site Changes

Both `lambda_handler.py` and `commands/run_backup.py` branch on `source_config.use_s3_batch`:

```python
if source_config.use_s3_batch:
    result = batch_backup_source(...)
    # log: "Batch job submitted: {job_id} ({objects_in_manifest} objects)"
else:
    result = backup_source(...)  # unchanged
```

`sync_bucket` and `backup_source` are **kept as-is** — still used for small buckets
and interactive CLI usage where immediate results are valuable.

---

## Files Changed

| File | Change |
|------|--------|
| `src/nzshm_backup/s3_batch.py` | **New** — `BatchJobResult`, `build_manifest_csv`, `write_manifest_to_s3`, `batch_backup_source` |
| `src/nzshm_backup/config/models.py` | Add `use_s3_batch` to `SourceConfig`, `s3_batch_role_arn` to `GeneralConfig`, cross-field validator |
| `src/nzshm_backup/lambda_handler.py` | Branch on `use_s3_batch` |
| `src/nzshm_backup/commands/run_backup.py` | Branch on `use_s3_batch` |
| `serverless.yml` | Add `s3control` + `iam:PassRole` to Lambda role |
| `backup-config.example.yaml` | Add new fields |
| `scripts/create-batch-role.py` | **New** — one-time IAM role creation |
| `tests/test_s3_batch.py` | **New** — unit tests for manifest generation and job submission |

---

## Risks

**Memory — manifest CSV at 8M objects**
At ~90 bytes/row × 8M rows = ~720 MB. Lambda is configured at 1024 MB.
Mitigation: stream rows to S3 via multipart upload (never hold the full CSV in memory).
On incremental runs the manifest is a tiny fraction of 8M rows — only first-run
or full-sync hits this limit.

**Listing time**
~16,000 `list_objects_v2` calls ≈ 160s at 8M objects. Well within 900s timeout.
Future mitigation if object count grows to 80M+: use S3 Inventory instead.

**Batch role is a manual pre-requisite**
`serverless deploy` cannot create it. Validate `s3_batch_role_arn` at startup
(`iam:GetRole`) and give a clear error if missing.

**Job status not visible in `backup status`**
After Lambda exits, job progress is only visible via `aws s3control describe-job`
or the batch report in S3. When `backup status` is implemented it should call
`s3control:ListJobs` / `s3control:DescribeJob`.

**ETag unreliability for multipart uploads**
Pre-existing issue inherited from `sync_bucket` — ETags for multipart-uploaded
objects are not content hashes. May cause unnecessary copies on first incremental
run after large multipart uploads. Acceptable.

**No atomicity**
Objects added to source after manifest generation are missed until the next run.
Same behaviour as current incremental sync — acceptable for backup-not-replication.

---

## Migration Steps

1. Deploy `s3_batch.py` + config changes with `use_s3_batch: false` everywhere (no behaviour change)
2. Create the batch IAM role via `scripts/create-batch-role.py`; set `s3_batch_role_arn` in config
3. Set `use_s3_batch: true` for `toshi` in config
4. Test: `backup --dry-run run --source toshi` (generates manifest, skips `create_job`)
5. First run: `backup run --source toshi` — submits batch job; monitor with:
   ```bash
   aws s3control describe-job --account-id ACCOUNT_ID --job-id JOB_ID --region ap-southeast-2
   ```
