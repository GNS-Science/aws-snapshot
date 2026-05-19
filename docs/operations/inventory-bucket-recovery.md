# Inventory Bucket Recovery Runbook

**Bucket:** `nzshm-backup-inventory-737696831915` (backup account `737696831915`, region `ap-southeast-2`)

**When to use this runbook:** the inventory bucket has been deleted,
its contents have been deleted or corrupted, or freshness checks in the
daily health report (ADR-005) are firing repeatedly.

**Severity:** HIGH for backup operations, LOW for backup data.
Existing backup data in `bb-*` per-source buckets is unaffected
throughout. DynamoDB PITR exports continue normally.

**Expected recovery time:** 24–48 hours. The recovery bound is set by
the S3 Inventory schedule (no on-demand API). Backup runs will fail for
that window.

---

## Quick reference

| What happened | Go to |
|---|---|
| Bucket deleted | [Scenario A](#scenario-a-bucket-deleted) |
| Bucket exists but `inventory/*` data deleted or corrupted | [Scenario B](#scenario-b-inventory-data-deleted-or-corrupted) |
| Bucket exists, only `_manifests/*` or `athena-results/*` lost | [Scenario C](#scenario-c-transient-data-lost) |
| Freshness alarm firing but bucket looks fine | [Diagnosis](#diagnosis-freshness-alarm-without-obvious-cause) |

---

## Pre-flight: confirm the scope

Before changing anything, run these checks. The recovery path depends
on what is actually broken.

```bash
# 1. Authenticate to the backup account
eval "$(aws configure export-credentials --profile nshm-backup-admin --format env)"
aws sts get-caller-identity   # confirm Account: 737696831915

# 2. Does the bucket exist?
aws s3api head-bucket --bucket nzshm-backup-inventory-737696831915 2>&1
# → exit 0 = exists; 404 = deleted; 403 = exists in another account

# 3. What top-level prefixes are present?
aws s3 ls s3://nzshm-backup-inventory-737696831915/
# expected: inventory/, athena-results/, _manifests/

# 4. Inventory freshness per source
for src in toshi ths static weka; do
  echo "=== $src ==="
  aws s3 ls s3://nzshm-backup-inventory-737696831915/inventory/$src/source/ --recursive \
    | tail -3
done
```

If `aws s3api head-bucket` returns 404 → Scenario A.
If `inventory/` is empty or contains only old/partial reports → Scenario B.
If `inventory/` is current but `_manifests/` or `athena-results/` is empty →
Scenario C.

---

## Scenario A: bucket deleted

### Symptoms

- `aws s3api head-bucket` returns 404.
- Backup Lambda invocations fail with `NoSuchBucket` on the Athena query
  result location, or Glue/Athena queries fail with "Table location
  s3://nzshm-backup-inventory-... is not accessible."
- Daily health report (post-ADR-005) reports all sources as red.

### Recovery

```bash
# 1. Recreate the bucket with the same name and region.
aws s3api create-bucket \
  --bucket nzshm-backup-inventory-737696831915 \
  --region ap-southeast-2 \
  --create-bucket-configuration LocationConstraint=ap-southeast-2

# 2. Re-apply the bucket policy that lets S3 Inventory write to it.
#    The canonical policy is built by setup-inventory.py and stored in
#    serverless.yml. Re-deploy or apply via:
aws s3api put-bucket-policy \
  --bucket nzshm-backup-inventory-737696831915 \
  --policy file://<path-to-saved-policy.json>

# 3. Enable versioning (per ADR-007).
aws s3api put-bucket-versioning \
  --bucket nzshm-backup-inventory-737696831915 \
  --versioning-configuration Status=Enabled

# 4. Re-apply lifecycle (30-day NoncurrentVersionExpiration).
aws s3api put-bucket-lifecycle-configuration \
  --bucket nzshm-backup-inventory-737696831915 \
  --lifecycle-configuration file://<saved-lifecycle.json>

# 5. Recreate the Glue database and tables. The scripted form lives in
#    setup-inventory.py — re-run it for each source.
uv run python scripts/setup-inventory.py --all-sources --recreate-glue-only

# 6. Verify Inventory configuration on each source/backup bucket is
#    still pointing at the recreated bucket. (It should be — Inventory
#    config is stored per-source-bucket, not on the destination.)
aws s3api list-bucket-inventory-configurations --bucket nzshm22-toshi-api-prod
# repeat for ths-dataset-prod, nzshm22-static-reports, nzshm22-weka-ui-prod
# and each bb-* backup bucket
```

### What happens next

- **Hour 0–24:** No Inventory reports arrive. Backup Lambda invocations
  fail at the diff step with "no inventory data." This is expected.
- **Hour 24–48:** First post-recovery Inventory report lands for each
  bucket on its scheduled cycle. Backup runs from that point succeed.
- **No manual replay needed.** The incremental sync naturally
  re-converges once Inventory data exists.

### Communicate the gap

Notify stakeholders that backup runs will fail-alarmingly (post-ADR-005)
for up to 48 hours. If the period overlaps a known DR-sensitive event,
restore operations can be done manually via direct `aws s3 cp` from
backup buckets — they do not need this inventory bucket.

---

## Scenario B: inventory data deleted or corrupted

### Symptoms

- Bucket exists; `aws s3 ls` works.
- `inventory/` prefix is empty, sparse, or contains files that produce
  Athena errors (`HIVE_BAD_DATA`, `Schema mismatch`, etc).
- Diffs run but produce implausible row counts (e.g. all objects
  flagged as new on a source that hasn't changed).

### Recovery

```bash
# 1. Stop the daily backup schedule so it does not run against bad data.
#    EventBridge rules are managed via the backup CLI.
uv run backup schedule disable --source toshi
uv run backup schedule disable --source ths
uv run backup schedule disable --source static
uv run backup schedule disable --source weka

# 2. Delete the entire inventory/ prefix.
aws s3 rm s3://nzshm-backup-inventory-737696831915/inventory/ --recursive

# 3. (Optional) Drop and recreate Glue tables to clear cached partition
#    metadata.
uv run python scripts/setup-inventory.py --all-sources --recreate-glue-only

# 4. Wait for the next scheduled Inventory run (24h window typically).
#    Monitor:
aws s3 ls s3://nzshm-backup-inventory-737696831915/inventory/ --recursive | tail
```

Once at least one fresh inventory report has landed for every source +
backup pair, re-enable the schedules:

```bash
uv run backup schedule enable --source toshi
uv run backup schedule enable --source ths
uv run backup schedule enable --source static
uv run backup schedule enable --source weka
```

### Why drop the data instead of trying to repair

The S3 Inventory format is rigid and the trustworthiness of a partial
or corrupted set is impossible to verify automatically. A wrong-but-
plausible Inventory produces wrong-but-plausible diffs, and the failure
mode is silent (objects either silently missed from backup or
unnecessarily re-copied). Starting clean costs 24h; trying to repair
risks corrupting *backup data state* in ways that are far more
expensive to undo.

---

## Scenario C: transient data lost

### Symptoms

- `inventory/` is intact and fresh.
- `_manifests/` or `athena-results/` is empty or has been pruned too
  aggressively.

### Recovery

No action required.

- `_manifests/unload/<source>/<bucket>/` is recreated every backup run
  (see #18). A failed run that leaves no manifest just means the next
  run starts clean.
- `_manifests/<run>.csv` files are recreated by the next backup run.
- `athena-results/` is rewritten by every Athena query.
- S3 Batch completion reports are historical and not used by any code
  path.

If a specific in-flight S3 Batch job's manifest was lost, that one job
fails — re-run that source's backup to regenerate the manifest and
submit a new Batch job.

---

## Diagnosis: freshness alarm without obvious cause

The ADR-005 daily health report flags Inventory data older than 30
hours. If this fires but the bucket and contents look intact:

```bash
# 1. When did the most recent Inventory report actually land per source?
for src in toshi ths static weka; do
  echo "=== $src source ==="
  aws s3 ls s3://nzshm-backup-inventory-737696831915/inventory/$src/source/ \
    --recursive | tail -1
  echo "=== $src backup ==="
  aws s3 ls s3://nzshm-backup-inventory-737696831915/inventory/$src/backup/ \
    --recursive | tail -1
done

# 2. Is the Inventory configuration still active on each source bucket?
aws s3api list-bucket-inventory-configurations --bucket nzshm22-toshi-api-prod
# Look for IsEnabled: true and Schedule.Frequency: Daily

# 3. Are the destination ARNs in the Inventory config still pointing at
#    the right bucket?  A bucket policy change can silently stop
#    Inventory writes without removing the config.
```

Common causes:

- Source bucket Inventory configuration was disabled or deleted (e.g.
  during unrelated source-account work).
- Inventory destination bucket policy was edited and no longer permits
  the source account / source ARN.
- Source bucket was renamed; Inventory continues writing under the old
  key path.

Fix the underlying cause; do not delete inventory data unless you have
confirmed corruption.

---

## Break-glass: intentional bucket deletion

ADR-007 adds a bucket policy DENY on `s3:DeleteBucket`. If the bucket
genuinely needs to be deleted (decommission, rebuild for unrelated
reasons), the policy must be removed first:

```bash
aws s3api delete-bucket-policy --bucket nzshm-backup-inventory-737696831915
# only then is `aws s3api delete-bucket` permitted
```

Re-applying the policy after recreate is part of [Scenario A](#scenario-a-bucket-deleted)
step 2 above.

---

## What this runbook does NOT cover

- **Loss of an actual backup bucket** (`bb-*`). That is a real DR event
  and is covered by `docs/design/disaster-recovery-scenario.md`. The
  inventory bucket is the control plane, not the data plane.
- **Loss of a source bucket.** That is a customer-account event; the
  backup system continues running and will eventually flag the source
  as missing via the inventory freshness check.
- **DynamoDB PITR recovery.** Independent of this bucket entirely; see
  `docs/design/dynamodb-pitr-watcher.md`.

---

## Related

- ADR-007 — the hardening decisions this runbook complements.
- ADR-005 / #16 — daily health report including the freshness watchdog
  that surfaces these scenarios in the first place.
- #18 — Athena UNLOAD cleanup race; depends on the Lambda role keeping
  delete permission on `_manifests/unload/*` after Scenario A recovery.
- `docs/design/disaster-recovery-scenario.md` — sibling runbook for the
  data-plane case.
