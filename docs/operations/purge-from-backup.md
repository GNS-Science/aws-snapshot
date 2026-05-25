# Manual purge from a backup bucket

The backup engine is **deliberately deaf to source deletions**: keys
deleted from a source bucket persist in the corresponding backup bucket
indefinitely. This is the trade required by
[ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
(no lifecycle expiry on current versions) and by the no-delete Lambda IAM
policy.

When orphan accumulation in a backup bucket is real and intentional —
e.g. NSHM cleaned up an experiment, or
[#24](https://github.com/GNS-Science/nzshm-backup/issues/24)-style
historic version bloat — this runbook is the only sanctioned path to
remove the orphans.

**This procedure is deliberately friction-laden.** It is not a daily
operation. Each invocation should leave an audit trail and a human
decision.

## When to use this

The trigger is a daily health-report line like:

```
ℹ backup has 12,431 orphans (source-side deletions retained per ADR-006)
```

Inspect the orphans before deciding to purge:

1. Run the inventory-diff CLI to obtain the actual orphan keys, not just
   the count:

   ```bash
   uv run backup ... TODO  # exact command added once exposed by ADR-009 CLI
   ```

2. Cross-check that source state really matches expectation — that the
   missing keys were *intentionally* removed from source, not lost due
   to a source-side incident you'd want recovered from backup.

If you cannot positively confirm the deletions were intentional,
**do not purge**. The orphans are protecting the data; you may need them.

## Required credentials

The Lambda execution role does **not** have `s3:DeleteObject` on backup
buckets and must not be granted it. The purge runs from an operator
workstation with admin credentials assumed for the duration:

```bash
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"
aws sts get-caller-identity   # confirm AdministratorAccess role
```

The admin profile is intentional: the same friction that prevents the
backup pipeline from deleting also requires a deliberate human step.

## Path A — small orphan list (under ~1,000 keys)

`aws s3api delete-objects` accepts up to 1,000 keys per call. For
small one-off cleanups this is the simplest path.

1. Prepare a manifest of keys to delete, one key per line:

   ```bash
   cat > /tmp/orphans.txt <<EOF
   experiments/2026-03/run-bad-1.h5
   experiments/2026-03/run-bad-2.h5
   ...
   EOF
   ```

2. Dry-run: confirm each key actually exists and that you have not
   miss-typed a prefix:

   ```bash
   while read -r KEY; do
     aws s3api head-object \
       --bucket bb-static-s3-static-reports-ap-southeast-2-210987654321 \
       --key "$KEY" \
       --query 'ContentLength' --output text || echo "MISSING: $KEY"
   done < /tmp/orphans.txt
   ```

3. Delete in a single batched call:

   ```bash
   jq -Rn '{Objects: [inputs | {Key: .}]}' /tmp/orphans.txt > /tmp/orphans.json
   aws s3api delete-objects \
     --bucket bb-static-s3-static-reports-ap-southeast-2-210987654321 \
     --delete file:///tmp/orphans.json
   ```

4. Record the audit trail (see *Audit trail* below).

## Path B — large orphan list (1,000+ keys, or version-scoped purge)

Use **S3 Batch Operations** with a manually-prepared CSV manifest.
Required for #24-style historic version bloat (millions of non-current
versions across two buckets).

1. Generate the manifest as `bucket,key[,versionId]` rows. For
   version-scoped purges (deleting specific non-current versions, keeping
   the current ones intact), the `versionId` column is mandatory:

   ```bash
   aws s3api list-object-versions \
     --bucket bb-static-s3-static-reports-ap-southeast-2-210987654321 \
     --query 'Versions[?IsLatest==`false`].[Key,VersionId,Size]' \
     --output text \
   | awk -F'\t' '{ printf "bb-static-...,%s,%s\n", $1, $2 }' \
   > /tmp/purge-manifest.csv
   wc -l /tmp/purge-manifest.csv     # sanity-check row count
   head -3 /tmp/purge-manifest.csv   # sanity-check shape
   ```

2. Upload the manifest to the inventory control bucket under a
   timestamped key so it survives audit:

   ```bash
   STAMP=$(date -u +%Y%m%dT%H%M%SZ)
   aws s3 cp /tmp/purge-manifest.csv \
     s3://nzshm-backup-inventory-210987654321/_purge-manifests/$STAMP.csv
   ```

3. Submit the S3 Batch Delete job, scoped to the affected backup bucket
   only:

   ```bash
   aws s3control create-job \
     --account-id 210987654321 \
     --operation '{"S3DeleteObject":{}}' \
     --report '{"Bucket":"arn:aws:s3:::nzshm-backup-inventory-210987654321","Prefix":"_purge-reports/","Format":"Report_CSV_20180820","Enabled":true,"ReportScope":"AllTasks"}' \
     --manifest "{\"Spec\":{\"Format\":\"S3BatchOperations_CSV_20180820\",\"Fields\":[\"Bucket\",\"Key\",\"VersionId\"]},\"Location\":{\"ObjectArn\":\"arn:aws:s3:::nzshm-backup-inventory-210987654321/_purge-manifests/$STAMP.csv\",\"ETag\":\"<paste-etag-from-step-2>\"}}" \
     --priority 10 \
     --role-arn arn:aws:iam::210987654321:role/nzshm-backup-batch \
     --no-confirmation-required \
     --description "Manual purge of orphans from bb-static — see audit note $STAMP"
   ```

4. Poll the job to completion and inspect the failure report:

   ```bash
   aws s3control describe-job --account-id 210987654321 --job-id <id>
   ```

5. Record the audit trail.

## Audit trail

Append an entry to `docs/PROD-DEPLOY-LOG.md` (or create a new
`docs/operations/purge-history.md` once we have more than one entry):

```markdown
### 2026-MM-DD — Manual purge of <N> orphans from bb-<bucket>

- Trigger: <link to health-report run that surfaced the orphans, or issue>
- Decision rationale: <why the deletions were intentional>
- Manifest: s3://nzshm-backup-inventory-210987654321/_purge-manifests/<STAMP>.csv
- Result: <N succeeded / M failed>; failure report at
  s3://nzshm-backup-inventory-210987654321/_purge-reports/<job-id>/
- Operator: <github handle>
```

The point of the audit entry is not bureaucracy — it is so the *next*
operator (which may be you, 18 months from now) can understand why N
objects vanished from the backup bucket without help from the lifecycle
policy.

## What this runbook deliberately does NOT do

- **No automation.** No cron, no Lambda, no slash-command shortcut.
  Every invocation is hand-driven by a named operator.
- **No partial-credential roles.** The backup-execution role never gets
  delete permissions; admin credentials are used and then released.
- **No interaction with the source bucket.** Source-side deletion is a
  separate decision made elsewhere; this runbook only touches the backup
  copy.

## Related

- [ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
  — why backup objects are kept forever
- [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md)
  — why orphan accumulation is class-2 informational, not an alarm
- [#24](https://github.com/GNS-Science/nzshm-backup/issues/24) — historic
  non-current-version bloat; a real candidate for Path B
