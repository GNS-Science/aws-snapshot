# Quick Start

This guide walks through the most common operations after [installation](installation.md).

## Command map

Top-level command groups:

```text
backup check      # pre-flight readiness checks
backup schedule   # EventBridge schedule management
backup setup      # infrastructure/bootstrap helpers
backup run        # trigger manual backup run
backup restore    # restore operations
backup test       # integrity/restore testing helpers
backup status     # latest backup status view
backup events     # event log view
backup config     # config view/push/pull/validate
```

For the full command tree and all options, see
[CLI Reference](../cli-reference.md).

## Prerequisites

- `backup-config.yaml` present in your working directory (or `BACKUP_CONFIG_PATH` set)
- AWS credentials exported in your shell (see [Installation](installation.md#aws-configuration))

## Check backup status

```bash
backup status
```

Shows the last backup run time, object counts, and any errors for all configured sources.

```bash
backup status --source toshi
backup status --output json
```

## Pre-flight check

Before running a backup for the first time, validate credentials and permissions:

```bash
backup check
backup check --source toshi
```

Fix any `FAIL` items before proceeding. `WARN` items (e.g. backup bucket doesn't exist yet)
are expected on first run and will be resolved automatically.

## Run a backup

Run for real:

```bash
backup run --source toshi
backup run --source all
```

For large buckets using S3 Batch Operations, the command submits the job and returns immediately.
Monitor progress with `backup restore status` or the S3 console.

## Force a full sync

By default, incremental runs skip objects whose ETag already matches the backup bucket.
To force a full re-copy (e.g. after a retention policy change):

```bash
backup run --source toshi --full-sync
```

## Restore from backup

```bash
# DynamoDB: restore to a new table at a specific point in time
backup restore run --source toshi \
    --tables ToshiAPI-FileTable \
    --to-point-in-time 2026-03-15T09:00:00Z

# S3: restore a full bucket
backup restore run --source toshi --buckets nzshm-toshi-api-data

# S3: restore a specific prefix only
backup restore run --source toshi \
    --buckets nzshm-toshi-api-data \
    --prefix models/2026/

# Check restore status
backup restore status --source toshi
```

Restores land in a `{source}-restore` bucket by default. See [Restore Operations](../user-guide/restore.md) for full details.

## Validate backup integrity

```bash
# Compare object counts and ETags between source and backup buckets
backup test integrity --source toshi

# Sample restore test (round-trip a small subset)
backup test restore --source toshi
```

## View and manage schedules

```bash
backup schedule show
backup schedule add --source toshi --frequency weekly --time 14:00
backup schedule remove --source toshi --frequency weekly
```

Times are in UTC. NZST = UTC+12, NZDT = UTC+13.

## Next steps

- [Configuration](configuration.md) — config file reference
- [User Guide: Backup](../user-guide/backup.md) — detailed backup options
- [User Guide: Restore](../user-guide/restore.md) — restore workflow and DR guidance
- [CLI Reference](../cli-reference.md) — full command reference
