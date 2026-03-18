# IAM Security Decisions

Documents IAM role design decisions with security rationale for audit and review.

---

## 1. Role split: reader vs restore

**Decision:** Two source-account roles — `nzshm-backup-reader` (backup direction) and
`nzshm-backup-restore` (restore direction) — rather than one combined role.

**Rationale:**
- Least-privilege: the Lambda that runs continuously for backup never has write permissions
  on source buckets; restore permissions are only live during explicit restore operations.
- Blast radius: a compromised backup process cannot overwrite source data.
- Audit trail: CloudTrail sessions are clearly labelled by role name, separating
  routine backup events from restore events.

---

## 2. Runtime vs setup-time bucket policy application

**Decision:** `restore run` applies `AllowNzshmBatchRoleWrite` to the target bucket at
runtime (just before Batch job submission) rather than relying solely on setup-time policy.

**Rationale:**
- Fewer steps during a real DR event reduces operator error risk.
- The target bucket name is known only at restore time (especially after truncation via
  `make_restore_bucket_name`); setup-time policy application cannot always predict it.
- Consistent with `ensure_dynamodb_backup_bucket_ready` in `dynamodb_backup.py`, which
  applies bucket policies at runtime, not setup-time.
- Setup-time write policy (via `create-source-roles.py`) is still applied as belt-and-
  suspenders, but runtime application is the authoritative safety net.

---

## 3. Restore role `s3:PutBucketPolicy` grant

**Decision:** `nzshm-backup-restore` is granted `s3:GetBucketPolicy` and `s3:PutBucketPolicy`
on source bucket ARNs and their canonical restore-target ARNs (`make_restore_bucket_name(b)`).

**Rationale:**
- Required for runtime policy application (see §2 above).
- Scope is narrow: only the specific source buckets listed in config, not all buckets.
- The `PutRestoreTargetPolicy` IAM statement in `build_restore_policy` enumerates exact
  bucket ARNs; wildcard resource is not used.
- This role is only assumed during explicit `backup restore run` invocations, not
  continuously, limiting the exposure window. Sessions are logged in CloudTrail.

---

## 4. Canonical restore-target naming (`make_restore_bucket_name`)

**Decision:** The restore-target bucket name is always `{source_bucket}-restore`, truncated
to 55 base chars so the total stays within S3's 63-character limit.

**Rationale:**
- One deterministic function (`make_restore_bucket_name`) is the single source of truth
  used by the CLI, IAM policy builder (`create-backup-roles.py`), and bucket policy setup
  (`create-source-roles.py`). Ad-hoc truncation (e.g. `-rest`) causes auth mismatches.
- Mirrors the truncation pattern in `make_restore_table_name` for DynamoDB.
- The `--original` flag on `restore run` bypasses this to restore into the original bucket
  for real DR scenarios.

---

## 5. Batch role scope (backup account)

**Decision:** `nzshm-backup-batch-role` uses a `bb-*-{region}-*` wildcard for backup bucket
resources rather than enumerating individual bucket ARNs.

**Rationale:**
- Backup buckets are provisioned automatically by the backup engine; enumerating them at
  role-creation time would require re-running `create-backup-roles.py` every time a new
  source is added.
- The backup account owns all `bb-*` buckets; source account bucket policies are the
  cross-account gate — a wildcard in the backup account IAM policy is not a significant
  additional risk.

---

## 6. DynamoDB restore role broad `dynamodb:*` grant

**Decision:** `ManageRestoredTables` grants `dynamodb:*` on all tables in the source account
rather than a narrow action list.

**Rationale:**
- `RestoreTableToPointInTime` performs undocumented internal operations (Scan, Query, …)
  on the restore target table. Narrowing by action leads to whack-a-mole with
  `AccessDeniedException` errors that are difficult to diagnose in production.
- Restored table names follow the `{original}-restore` convention, which is not in the
  configured table list; resource-level scoping to known ARNs is not possible without
  accepting false denials.
- Mitigation: this role is only assumed during explicit restore operations, not
  continuously, and all actions are recorded in CloudTrail.
