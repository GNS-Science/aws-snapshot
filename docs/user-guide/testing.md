# Testing & Validation

The `backup test` subcommand provides integrity checks and round-trip restore
tests to validate that backups are readable and consistent.

## Feature status

| Feature | Status | Notes |
|---------|--------|-------|
| `test integrity` — S3 object comparison | ✅ Implemented | Full ETag diff; excludes operational prefixes |
| `test integrity` — DynamoDB PITR check | ✅ Implemented | Read-only; checks PITR enabled + completed exports exist |
| `test restore` — S3 direct copy sample | ✅ Implemented | Copies N objects to temp bucket, verifies ETags, cleans up |
| `test restore` — S3 Batch Operations path | ✅ Implemented | `--use-batch`; validates production IAM + Batch pipeline |
| `test restore` — DynamoDB restorability | ✅ Implemented | Read-only; checks PITR + export bucket accessible |
| Event log (`test_restore` events) | ✅ Implemented | Emits passed/failed/etag_mismatch to `_events/` |
| `test full-drill` | ⏳ Not yet implemented | Planned quarterly DR drill |
| Automated scheduling via EventBridge | ⏳ Not yet implemented | Must be triggered manually for now |
| Glacier/Deep Archive object test path | ⏳ Not yet implemented | Archived objects are skipped in sample restore |

---

## `backup test integrity`

Validates that backup data matches source without performing a restore.

```bash
backup test integrity --source arkivalist
```

**S3 buckets:**

- Compares every non-operational object in the source bucket against the backup
- Flags **missing objects** (in source but not in backup)
- Flags **ETag mismatches** (possible backup poisoning — source mutation propagated to backup)
- Objects in backup but not in source are intentionally **not** flagged — the backup retains deleted
  objects until the lifecycle policy expires them
- Operational prefixes excluded from both sides: `_state/`, `_manifests/`, `_batch-reports/`, `_events/`

**DynamoDB tables:**

- Confirms PITR is enabled on each table (prerequisite for point-in-time restore)
- Counts all completed exports (paginated — shows the real total, not a capped number)
- Shows latest export timestamp

Exits with code 1 if any discrepancy or missing protection is found.

---

## `backup test restore`

Exercises the actual restore path on a small sample without a full restore.

```bash
# S3 sample restore — direct copy (default)
backup test restore --source arkivalist

# S3 sample restore — S3 Batch Operations path (validates production IAM + code path)
backup test restore --source arkivalist --use-batch

# Control sample size
backup test restore --source arkivalist --sample-size 20
```

**S3 testing:**

1. Samples N objects from each backup bucket (default 10; reduced if fewer available)
2. Creates a temporary bucket (`bb-restore-test-{ts}-{account_id}`)
3. Copies objects via direct copy or S3 Batch Operations
4. Verifies ETags match the backup
5. Deletes the temporary bucket (always, even on failure)

Objects in archived storage tiers (Glacier, Glacier IR, Deep Archive) are automatically skipped —
they require a separate restore request before they can be copied.

**DynamoDB testing (read-only, runs even with `--dry-run`):**

- Confirms PITR is enabled (prerequisite for point-in-time restore)
- Checks the export bucket has accessible data

A `test_restore` event (result: `passed`, `failed`, or `etag_mismatch`) is appended to the event
log in the backup bucket.

Exits with code 1 if any check fails.

---

## `backup test full-drill`

Not yet implemented. Planned to run a full quarterly disaster recovery drill (restore + validate
entire dataset to an isolated environment).

---

## When to run tests

| Test | Recommended cadence |
|------|---------------------|
| `test integrity` | Before each DR drill; after any bulk backup run |
| `test restore` (direct copy) | Weekly |
| `test restore --use-batch` | Monthly — validates the production restore IAM path |
| `test full-drill` | Quarterly (once implemented) |
