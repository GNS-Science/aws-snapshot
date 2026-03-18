# Disaster Recovery Scenario: Full PROD Account Compromise

## Scenario

A malicious attacker gains access to the NSHM PROD account (`210987654321`) and
deletes every S3 bucket and every DynamoDB table. The breach is detected within
24 hours. The backup account (`345678901234`) was **not** accessed.

**Deleted from PROD:**
- `nzshm-toshi-api-data` S3 bucket (8 TB)
- `ths-dataset-prod` S3 bucket (1 TB)
- `ToshiFileObject-PROD`, `ToshiIdentity-PROD`, `ToshiTableObject-PROD`, `ToshiThingObject-PROD` DynamoDB tables

**Safe in backup account (separate blast radius):**
- `bb-toshi-s3-api-ap-southeast-2-345678901234` — last weekly S3 backup
- `bb-ths-s3-dataset-ap-southeast-2-345678901234` — last weekly S3 backup
- `bb-toshi-dynamo-ap-southeast-2-345678901234` — last monthly DynamoDB export
- DynamoDB PITR stream — AWS retains this for **35 days after table deletion**, recoverable to any second

---

## Data currency at time of recovery

| Data | Method | Max data loss |
|------|--------|--------------|
| ToshiAPI DynamoDB | PITR (if < 35 days since deletion) | **~0 — recover to the second before attack** |
| ToshiAPI DynamoDB | Monthly S3 export (fallback) | Up to 28 days |
| ToshiBucket S3 (8 TB) | Weekly backup | Up to 7 days of new BLOBs |
| THS S3 (1 TB) | Weekly backup | Up to 7 days of new BLOBs |

S3 data loss is bounded because BLOBs are write-once — any object that was
backed up is intact and unchanged. Only objects added in the last week between
backup runs would be missing.

---

## Phase 0: Contain (0–2 hours)

Regardless of which restore path is chosen, do this first:

1. **Revoke all PROD account credentials** — rotate root account keys, delete
   or disable all IAM users and access keys, invalidate all active sessions
2. **Verify backup account is clean** — check CloudTrail in `345678901234`
   for any suspicious access; confirm backup buckets are intact with
   `backup status --source toshi --source ths`
3. **Assess backup currency** — note the timestamp of the last successful
   backup run from CloudWatch logs (`/aws/lambda/nzshm-backup-dev`) or S3 Inventory reports
4. **Decide restore destination** — PROD account (Option A) or new account
   (Option B). See trade-offs below.
5. **Notify stakeholders** — set expectations on RTO (24–72 hours, S3 is
   the bottleneck)

---

## Phase 1: DynamoDB recovery (2–10 hours)

DynamoDB can be recovered in parallel with S3 and completes much faster.

### Preferred: PITR restore (recover to second before attack)

AWS retains PITR data for 35 days after table deletion. Restore all tables
with a single CLI command:

```bash
# Restore all configured tables to a point just before the attack
# (substitute the actual attack timestamp)
BACKUP_CONFIG_PATH=backup-config.yaml \
  backup restore run \
    --source toshi \
    --to-point-in-time "2026-03-15T09:00:00Z"

# Monitor progress:
BACKUP_CONFIG_PATH=backup-config.yaml \
  backup restore status --source toshi
```

- Each table restore runs independently and in parallel
- Typical duration: 2–8 hours per table depending on size
- PITR is re-enabled automatically on each table once it reaches ACTIVE
  (via `pitr-watcher` Lambda — see `docs/design/dynamodb-pitr-watcher.md`)

### Fallback: import from S3 export (if PITR > 35 days)

If the breach went undetected for more than 35 days (PITR window expired):

```bash
# Grant the target account access to the export bucket in backup account,
# then import from the most recent monthly export:
aws dynamodb import-table \
    --s3-bucket-source S3Bucket=bb-toshi-dynamo-ap-southeast-2-345678901234,\
S3KeyPrefix=dynamodb-exports/ToshiFileObject-PROD/2026/03/01 \
    --input-format DYNAMODB_JSON \
    --table-creation-parameters \
        TableName=ToshiFileObject-PROD,\
        BillingMode=PAY_PER_REQUEST \
    --region ap-southeast-2
```

- Data will be at most 28 days stale
- Re-enable PITR after import

---

## Phase 2: S3 recovery (12–48 hours, parallel with Phase 1)

S3 is the bottleneck — 9 TB of data must be copied from backup to target.
No native "PITR" equivalent exists for S3; the backup bucket IS the archive.

```bash
# Ensure target buckets exist first — S3 bucket names are permanent so the
# target is always the original bucket name. Recreate if accidentally deleted:
#   aws s3api create-bucket --bucket nzshm-toshi-api-data --region ap-southeast-2 \
#       --create-bucket-configuration LocationConstraint=ap-southeast-2

# Submit a Batch Operations restore job for each bucket.
# Jobs run server-side — no long-lived process required.
backup restore run --source toshi --buckets toshi-api-data
backup restore run --source ths --buckets ths-dataset

# Monitor progress:
backup restore status --source toshi
backup restore status --source ths
```

`backup restore run` uses S3 Batch Operations (see `docs/design/s3-restore-strategy.md`).
Jobs run server-side, handle retries automatically, and produce a per-object completion
report. Target buckets must already exist before submitting.

**Estimated duration:** 12–48 hours depending on object count and sizes.
S3 intra-region copy throughput varies; large BLOB files (HDF5, NetCDF) copy
faster per-GB than many small objects.

---

## Restore destination options

### Option A: Restore into PROD account (after lockdown)

**Pros:**
- All existing service integrations, API endpoints, IAM cross-account references,
  and DNS records continue to work unchanged
- No need to update downstream consumers

**Cons:**
- Requires full security audit of the compromised account before trusting it
- Risk of persistent attacker presence (backdoor IAM roles, Lambda functions,
  scheduled events) — must be thoroughly reviewed
- CloudTrail may have been tampered with

**Steps:**
1. Complete Phase 0 credential revocation
2. Full CloudTrail audit — identify every action taken by the attacker
3. Remove any backdoors (rogue IAM roles, users, Lambda functions, EventBridge rules)
4. Recreate S3 buckets and DynamoDB tables with original names
5. Restore data (Phase 1 + Phase 2 above)
6. Re-enable backup Lambda cross-account role for PROD

### Option B: Restore into a new account (clean slate)

**Pros:**
- No risk of persistent attacker presence
- Clean security baseline
- Can run in parallel with PROD account forensics

**Cons:**
- All service integrations must be updated to point to new account/buckets
- New IAM cross-account roles must be created (`scripts/create-reader-role.py`)
- DNS, API endpoints, CDN origins need updating
- New AWS account setup takes 1–4 hours

**Steps:**
1. Create new AWS account under Organizations
2. Recreate S3 buckets and DynamoDB tables (can use different names initially)
3. Restore data (Phase 1 + Phase 2, targeting new account)
4. Update all service references pointing to PROD resources
5. Run `scripts/create-source-roles.py` in new account to re-establish backup path
6. Decommission old PROD account after forensic review

---

## Recovery time objective (RTO) summary

| Component | PITR path | Export path |
|-----------|-----------|-------------|
| DynamoDB (all tables) | 4–8 hours | 6–12 hours |
| S3 ToshiBucket (8 TB) | 12–48 hours | 12–48 hours |
| S3 THS (1 TB) | 2–6 hours | 2–6 hours |
| Account lockdown + audit | 2–4 hours | 2–4 hours |
| **Total RTO** | **~24–48 hours** | **~24–48 hours** |

S3 copy dominates in both cases. DynamoDB completes well before S3.

---

## Current implementation gaps

These capabilities are not yet implemented and would need to be built or done
manually during a real incident:

| Gap | Impact | Notes |
|-----|--------|-------|
| S3 restore uses direct copy_object | Not suitable for 8 TB ToshiBucket | S3 Batch Operations implementation pending — see `docs/design/s3-restore-strategy.md` |
| No `import-table-from-s3` integration | PITR fallback requires manual CLI | Low priority if PITR window covers 35 days |
| No restore runbook tested | Unknown failure modes | Quarterly DR drill (Phase 5) needed |

---

## Prevention notes

This scenario is mitigated by the account isolation design — the backup account
being a separate blast radius means an attacker with PROD credentials cannot
reach backups. Key controls that make this work:

- Backup Lambda role has no `s3:DeleteObject` — backups cannot be deleted via the Lambda
- Backup account credentials are separate from PROD credentials
- PITR is enabled per-table and survives table deletion for 35 days
- Backup buckets tagged `ManagedBy=nzshm-backup` with delete-protection policy

**Created:** 2026-03-16
