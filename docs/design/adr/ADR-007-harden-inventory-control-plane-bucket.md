# ADR-007: Harden the inventory bucket as a critical control-plane dependency

- Status: Proposed
- Date: 2026-05-19

## Context

The bucket `nzshm-backup-inventory-123456789012` (in the backup account
`123456789012`) is the **control plane** for the entire backup system.
Despite its criticality, it has weaker protections than the per-source
backup buckets that hold the actual data.

### What lives in the inventory bucket

| Prefix | Contents | Regeneratable? |
|---|---|---|
| `inventory/<source>/source/<bucket>/` | Daily S3 Inventory reports for source buckets | Yes — next scheduled run (24–48h) |
| `inventory/<source>/backup/<bb-...>/` | Daily S3 Inventory reports for backup buckets | Yes — same |
| `athena-results/` | Athena query outputs (transient) | Yes — trivially |
| `_manifests/unload/<source>/<bucket>/` | Athena UNLOAD intermediates (per run) | Yes — trivially |
| `_manifests/<run>.csv` | S3 Batch CopyObject input manifests | Yes — regenerated each run |
| Batch completion reports | Per-run S3 Batch reports | Lost; historical only |

### What does NOT live here

- Actual backup data — lives in the per-source `bb-*` buckets, unaffected.
- DynamoDB PITR exports — separate `bb-*-dynamo-*` buckets.
- Lifecycle/replication configurations — stored on each individual bucket.

### Impact of bucket loss

If the inventory bucket is deleted or corrupted, the following stop
working:

- **All daily backups.** `athena_inventory.build_inventory_manifest_via_athena`
  fails at the diff step.
- **`backup status` inventory-freshness checks.**
- **S3 Batch restore.** Restore manifests are written here
  (`s3_batch.py:537`), so a DR event during the recovery gap can't use
  the Batch restore path (direct `aws s3 cp` from backup buckets still
  works manually).
- **`test restore` / `test integrity`.** Both sample via Athena.

The actual backup data is preserved throughout, and DynamoDB PITR
exports continue unaffected.

### Current protection gaps

Verified on 2026-05-19 against the live bucket:

| Protection | Backup buckets (`bb-*`) | Inventory bucket | Gap |
|---|---|---|---|
| Versioning | Enabled | **Not enabled** | An accidental `aws s3 rm --recursive` is unrecoverable |
| IAM `s3:DeleteObject` denied to Lambda | Yes | No explicit deny | The Lambda role can delete inventory contents |
| Bucket policy DENY on `s3:DeleteBucket` | No (not needed — versioning + no-delete protects data) | None | Bucket itself can be deleted by any account admin |
| Documented recovery runbook | DR scenario doc | **None** | Operator under pressure has no checklist |
| Freshness watchdog | N/A | None | Corruption (vs deletion) is silent — diffs run against stale data |

### Recovery is possible, but slow

Even with a clean recovery path, the **exposure window is 24–48 hours**:
that is how long S3 takes to produce the first new Inventory report
after the bucket is recreated. During this window backups silently fail
(or, post-#16, alert and fail). The fix is therefore prevention, not
faster recovery — there is no AWS API to force an on-demand Inventory
run.

## Decision

Apply four hardening measures and document the recovery procedure:

### 1. Enable versioning on the inventory bucket

Apply `s3:PutBucketVersioning` with `Status: Enabled`. Add a lifecycle
rule `NoncurrentVersionExpiration: Days: 30` so versioning does not
unbounded the storage cost (inventory data is high-volume and
regeneratable; a 30-day window comfortably covers any "I just deleted
the wrong thing" recovery).

### 2. Bucket policy DENY on `s3:DeleteBucket`

Add a top-level Deny statement that applies to all principals except an
explicitly-named break-glass role (or no exception at all — the bucket
should not need to be deleted as part of normal operations). This
provides defence against `aws s3api delete-bucket` issued from an admin
session.

```json
{
  "Sid": "DenyBucketDeletion",
  "Effect": "Deny",
  "Principal": "*",
  "Action": "s3:DeleteBucket",
  "Resource": "arn:aws:s3:::nzshm-backup-inventory-123456789012"
}
```

### 3. Restrict object deletion to the admin path

The backup Lambda role legitimately needs `s3:DeleteObject` on this
bucket (for `_cleanup_unload_parts` and Athena temp file cleanup), so a
blanket Deny like the one on backup buckets is not appropriate. Instead:

- Scope Lambda's delete permission to `_manifests/*` and `athena-results/*`.
- Deny delete on `inventory/*` from the Lambda role explicitly.
- Inventory data can only be deleted by a break-glass admin role.

### 4. Freshness watchdog in the daily health report

Add an assertion to the daily health report (ADR-005 / #16): for each
source, the most recent inventory report under
`inventory/<source>/source/<bucket>/` must be ≤ 30 hours old. If older,
flag loud in Slack and email. This is the corruption-detection signal —
a silently stale inventory bucket no longer goes unnoticed.

> **Per [ADR-009](ADR-009-health-check-measurement-model.md):** this
> signal is *class 3* (forward-looking risk → yellow), distinct from the
> class-1 (red) signals that mean the backup system has actually failed.
> An entirely missing inventory remains class 1 (the health report
> cannot determine state), but mere staleness is yellow.

### 5. Document the recovery runbook

Write `docs/operations/inventory-bucket-recovery.md` covering both
deletion and corruption scenarios, with the exact command sequence and
the expected 24–48h exposure window.

## Alternatives considered

1. **Replicate the inventory bucket to a second region.** S3 Cross-Region
   Replication would give a warm spare. Rejected as overkill — the data
   regenerates from S3 service config in 24–48h, and the actual backup
   data (which is what matters) is already protected. CRR also costs
   meaningfully more than the contents are worth.
2. **Force on-demand Inventory generation as part of recovery.** Not
   possible — S3 Inventory is a scheduled service with no on-demand API.
   Recovery time is fundamentally bounded by the next Inventory run.
3. **Move manifests/results to a separate bucket from Inventory data.**
   Cleaner separation but doubles the number of buckets to protect and
   does not materially improve recovery. Rejected as not worth the
   refactor.
4. **Do nothing — accept the gap.** The bucket has not been lost in the
   ~2 months it has existed. But the protection gap is asymmetric: the
   downside of losing it is days of failed backups for the entire
   system, and the cost of these mitigations is trivial.

## Implementation scope

| Component | File / target | Effort |
|-----------|---------------|--------|
| Enable versioning + 30-day NoncurrentVersionExpiration | `serverless.yml` or `create-backup-roles.py` (wherever the bucket is provisioned) | Trivial |
| Bucket policy DENY on `s3:DeleteBucket` | same | Trivial |
| Scoped Lambda delete permissions | `serverless.yml` IAM role | Small |
| Freshness watchdog in health report | `src/aws_snapshot/health_report.py` (depends on #16) | Small |
| Recovery runbook | `docs/operations/inventory-bucket-recovery.md` (new — drafted alongside this ADR) | Small |
| Apply changes to deployed bucket | One-off `aws s3api put-bucket-versioning` + `put-bucket-policy` | Trivial |

## Risks

- **Lambda delete-permission scoping mistake.** If Lambda loses the
  ability to clean up `_manifests/unload/*`, ADR-018-style stale-file
  races become permanent. The implementation must keep Lambda's delete
  ability for the cleanup prefixes; only `inventory/*` is locked.
- **Break-glass role gap.** A bucket DENY on `s3:DeleteBucket` that
  blocks *everyone* means an authorised teardown (e.g. an actual
  decommission years from now) requires policy removal first. Document
  this as part of the runbook.

## Links

- Recovery runbook: `docs/operations/inventory-bucket-recovery.md`
- Related: ADR-005 / #16 (freshness watchdog hosted here)
- Related: #18 (Athena UNLOAD cleanup — depends on Lambda retaining
  delete on `_manifests/unload/*`)
- DR scenario: `docs/design/disaster-recovery-scenario.md` (does not
  currently mention the inventory bucket — should be updated as part of
  implementing this ADR)
