# NSHM Backup Solution

AWS-native backup management CLI for NSHM datasets (ToshiAPI and THS).
Replaces AWS Backup (~$1,700 NZD/month) with S3 Glacier lifecycle policies
and DynamoDB Point-in-Time exports (~$618 NZD/month target).

## Implementation Status

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 Step 1 | ✅ Complete | CLI skeleton with Typer |
| Phase 1 Step 2 | ✅ Complete | Config system + S3 backup operations |
| Phase 2        | ✅ Complete | DynamoDB PITR export + EventBridge scheduling |
| Phase 3        | ✅ Complete (notifications); cost reporting outstanding | Slack + SNS-email; daily health report; Lambda-error alarm (ADR-005) |
| Phase 4        | ✅ Substantially complete | Restore (S3 + DynamoDB, cross-account) |
| Phase 5        | ✅ Core done | Testing, validation, event audit log |
| Phase 6        | 🔄 In progress | Parallel run, NSHM cutover (arkivalist done) |

**Tests:** 403 passing · **Coverage:** 77% · **Lint:** ruff clean

---

## Features (implemented)

- **Configuration**: YAML config with Pydantic validation, alias→ARN mapping
- **S3 Backup**: Incremental sync with 3-tier lifecycle policies (Standard → Glacier Instant → Deep Archive)
- **DynamoDB Backup**: Point-in-Time export to S3, idempotent export bucket setup
- **EventBridge Scheduling**: Create/enable/disable weekly/daily/hourly rules; localised time input and display
- **Lambda Handler**: EventBridge-triggered backup orchestration (S3 + DynamoDB)
- **Restore**: S3 (direct copy + S3 Batch Operations) and DynamoDB PITR restore with async status tracking
- **Testing**: `backup test integrity` (ETag diff + PITR check) and `backup test restore` (sample restore)
- **Event audit log**: Append-only JSONL log in backup bucket (`_events/`) — all backup/restore events recorded
- **Daily health report** (`backup health-report`): per-source status + inventory freshness + object-count delta + sampled restore verification, delivered to Slack and SNS-email. Fires automatically at 14:30 NZST; canary (weka) tested daily, large sources rotated through Mon/Wed/Fri. See [docs/user-guide/health-report.md](docs/user-guide/health-report.md).
- **Lambda-error alarm**: CloudWatch alarm on backup Lambda `Errors` → SNS → email, fires within ~5 min of any hard failure. Complementary to the daily report (ADR-005 fast path).
- **YAML-managed notification recipients**: `notifications.alerts.emails` + `notifications.reports.email.addresses` lists in `backup-config.yaml`; `backup notifications apply` reconciles SNS subscriptions to match. See [docs/operations/enabling-notifications.md](docs/operations/enabling-notifications.md).
- **Dry-run mode**: All mutating operations support `--dry-run`
- **JSON output**: `--output json` for scripting
- **Localised timestamps**: CLI input/output in NZDT/NZST/AEST/AEDT

---

## Installation

```bash
uv sync --all-extras      # installs all deps including dev and docs extras
```

The `backup` command is registered as a console script and available immediately after install.

---

## Usage

### Global flags

```bash
backup --help
backup --dry-run run --source toshi     # Simulate without executing
backup --verbose run --source all       # Detailed logging
backup --output json schedule show      # Machine-readable output
```

### Configuration

```bash
backup config show                      # Display full loaded config
backup config validate                  # Validate backup-config.yaml
backup config show --key retention      # Show a specific section
```

### Run backup

```bash
backup run --source toshi               # S3 sync + DynamoDB PITR export for toshi
backup run --source ths                 # S3 sync for ths
backup run --source all                 # All sources
backup run --source toshi --full-sync   # Force full copy (skip ETag check)
backup --dry-run run --source toshi     # Preview without executing
```

### Schedule management

```bash
backup schedule show                    # List EventBridge rules with localised run times

# --time accepts UTC (HH:MM), localised (HH:MM TZ), or full datetime (YYYY-MM-DD HH:MM TZ)
backup schedule add --source toshi --frequency weekly --time '02:00 NZST'
backup schedule add --source toshi --frequency weekly --time '2026-03-29 12:15 NZDT'  # day-of-week from date
backup schedule add --source toshi --frequency daily  --time '01:00 NZST'
backup schedule add --source toshi --frequency hourly --time '00:30'       # :30 past each hour

backup schedule enable --source toshi                      # Enable all rules for toshi
backup schedule enable --source toshi --frequency weekly   # Enable weekly only
backup schedule disable --source toshi                     # Disable all rules for toshi
backup schedule remove --source toshi --frequency daily    # Delete rule entirely
```

### Restore

```bash
backup restore run --source toshi --buckets nzshm-toshi-api-data
backup restore run --source toshi --tables ToshiAPI-FileTable --to-point-in-time '2026-03-25 07:50 NZDT'
backup restore status --source toshi
```

### Testing

```bash
backup test integrity --source toshi          # ETag diff + PITR check
backup test restore --source toshi            # Sample restore (direct copy)
backup test restore --source toshi --use-batch  # Sample restore via S3 Batch Operations
```

### Event audit log

```bash
backup events --source toshi                  # Show recent backup/restore events
backup events --source toshi --limit 50
```

### Status & reporting (Phase 3 — not yet implemented)

```bash
backup status
backup report --period 30d
backup costs predict
```

---

## Configuration

Copy `backup-config.example.yaml` to `backup-config.yaml` and fill in your account ID and resource names:

```yaml
general:
  region: ap-southeast-2
  environment: production
  lambda_arn: null          # Set after first serverless deploy

sources:
  toshi:
    display_name: "ToshiAPI"
    s3_buckets:
      - arn:aws:s3:::YOUR-TOSHI-BUCKET-NAME
    dynamodb_tables:
      - arn:aws:dynamodb:ap-southeast-2:ACCOUNT_ID:table/ToshiAPI-FileTable
      - arn:aws:dynamodb:ap-southeast-2:ACCOUNT_ID:table/ToshiAPI-ThingTable
    dynamodb_export_format: DYNAMODB_JSON

  ths:
    display_name: "THS_dataset_prod"
    s3_buckets:
      - arn:aws:s3:::YOUR-THS-BUCKET-NAME
    dynamodb_export_format: DYNAMODB_JSON

retention:
  hot_days: 30      # S3 Standard
  warm_days: 90     # Glacier Instant
  cold_days: 365    # Deep Archive
  max_age_days: 365

restore:
  auto_approve_threshold: 100    # NZD — auto-approve below this
  dual_approval_threshold: 500   # NZD — two approvers above this
```

---

## Deployment

### Prerequisites

```bash
npm install -g serverless
uv sync --all-extras
cp backup-config.example.yaml backup-config.yaml   # edit with real values
```

### Deploy Lambda

```bash
serverless deploy                  # Deploy to AWS
serverless deploy --stage prod     # Production stage

# After deploy, update lambda_arn in backup-config.yaml, then re-deploy
# to wire up EventBridge targets.
```

### Add schedules after deploy

```bash
backup schedule add --source toshi --frequency weekly --time 14:00
backup schedule add --source ths   --frequency weekly --time 14:00
backup schedule show
```

---

## Sandbox testing

See [`scripts/sandbox_setup.sh`](scripts/sandbox_setup.sh) — creates lightweight source
resources (S3 buckets + DynamoDB tables with PITR, seeded with sample data) in a sandbox
AWS account so you can run `backup run` and `backup schedule` against real AWS without
touching production.

```bash
# One-time setup
scripts/sandbox_setup.sh setup

# Run backup against sandbox resources
backup run --source toshi
backup run --source all --dry-run

# Tear down all sandbox resources when done
scripts/sandbox_setup.sh teardown
```

See [`backup-config.sandbox.yaml`](backup-config.sandbox.yaml) for the matching config.

---

## Development

```bash
make test                 # All tests with coverage
make lint                 # ruff + mypy
make fmt                  # ruff format + ruff --fix
make check                # lint then test
make upgrade              # upgrade deps (1-week safety margin)

uv run pytest tests/test_foo.py   # single file
```

---

## Architecture

```
EventBridge (cron) → Lambda (nzshm-backup)
                         ├── S3 sync → backup bucket (Standard → Glacier → Deep Archive)
                         └── DynamoDB PITR export → export bucket (same lifecycle)
```

**Backup bucket naming:**
- S3: `{source-bucket}-backup-{region}-{account_id}`
- DynamoDB export: `nzshm-dynamo-backup-{source}-{region}-{account_id}`

**IAM:** Lambda has no `s3:DeleteObject` permission. Lifecycle expiration still fires.
DynamoDB restores always go to a new table (never overwrite in-place).

---

## Documentation

- [Design Plan & Cost Analysis](docs/design/backup-solution-plan.md)
- [Typer rationale](docs/design/TYPER_RATIONALE.md)

## License

MIT
