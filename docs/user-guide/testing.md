# Testing & Validation

The `backup test` subcommand provides integrity checks and round-trip restore
tests to validate that backups are readable and consistent.

## Integrity check

Compares the source and backup buckets/tables without restoring anything:

```bash
backup test integrity --source toshi
```

For S3 buckets this compares:
- Object counts (source vs backup)
- ETag values for a sample of objects

For DynamoDB tables this checks:
- Whether a recent export exists in the backup bucket
- Export metadata (item count, status)

Useful before a DR drill to confirm the backup is in good shape.

## Sample restore test

Exercises the actual restore path on a small sample:

```bash
# S3 sample restore (downloads and verifies a subset of objects)
backup test restore --source toshi

# Force S3 Batch Operations path for the sample (tests IAM + Batch pipeline)
backup test restore --source toshi --use-batch

# DynamoDB round-trip restore (submit + wait + verify item count)
backup test restore --source toshi --tables
```

The sample restore:
1. Selects a representative sample of objects from each backup bucket
2. Copies them to a temporary restore bucket
3. Verifies object size and ETag against the backup
4. Cleans up the temporary bucket

Results are printed to stdout and can be captured as JSON with `--output json`.

## When to run tests

| Test | Recommended cadence |
|------|---------------------|
| `test integrity` | Before each DR drill; after any bulk backup run |
| `test restore` (S3 sample) | Weekly (can be automated via EventBridge) |
| `test restore --tables` | Monthly; before any production DynamoDB restore |
| Full DR drill | Quarterly — restore + validate entire dataset |

## Automated test scheduling

Test runs can be triggered by a separate EventBridge rule targeting the Lambda.
Configuration in `testing` block of `backup-config.yaml`:

```yaml
testing:
  weekly_small_test:
    enabled: true
    day: wednesday
    time: "10:00"
    sample_size_mb: 100
  monthly_table_restore:
    enabled: true
    day: first-monday
    time: "09:00"
    table: ToshiAPI-FileTable
  quarterly_full_drill:
    enabled: true
    months: [january, april, july, october]
    day: 15
    isolated_environment: true
```

!!! note
    Automated test scheduling via EventBridge is planned but not yet wired up.
    Currently tests must be triggered manually from the CLI.
