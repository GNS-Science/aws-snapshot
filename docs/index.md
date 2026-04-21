# NSHM Backup Solution

AWS-native backup management CLI for NSHM datasets (ToshiAPI and THS).

## Features

- **Automated Scheduling**: Weekly/daily backup schedules via AWS EventBridge
- **S3 Backup**: Efficient backup with S3 Glacier lifecycle policies
- **DynamoDB Export**: Point-in-time exports to S3
- **Restore Operations**: Preview and execute restores with cost estimation
- **Automated Testing**: Weekly/monthly/quarterly restore validation
- **Cost Tracking**: Real-time cost monitoring and reporting
- **Notifications**: SES email and Slack integration

## Quick Start

```bash
# Install with Poetry
poetry install

# Activate virtual environment
poetry shell

# Run the CLI
backup --help
```

## Key Commands

```bash
# Show backup status
backup status

# Run manual backup
backup run --source toshi --dry-run

# List restore points
backup restore list --limit 10

# Preview restore with cost estimate
backup restore preview --date 2026-02-15 --source toshi

# Show schedule
backup schedule show

# Provision inventory for a source
backup setup inventory --source ths --source-profile nshm-admin --backup-profile nshm-backup-admin

# Generate cost report
backup costs report --period last-month
```

## Documentation

### Getting Started
- [Installation](getting-started/installation.md)
- [Quick Start](getting-started/quickstart.md)
- [Configuration](getting-started/configuration.md)

### User Guide
- [Backup Operations](user-guide/backup.md)
- [Restore Operations](user-guide/restore.md)
- [Testing & Validation](user-guide/testing.md)
- [Cost Management](user-guide/costs.md)

### CLI Reference
- [Complete CLI Documentation](cli-reference.md) (includes quick command tree + auto-generated details)

### Architecture
- [Overview](architecture/overview.md)
- [Cost Model](architecture/cost-model.md)
- [Storage Tiers](architecture/storage-tiers.md)

### Design Documents
- [Backup Solution Plan](design/backup-solution-plan.md)
- [S3 Manifest Bottleneck](design/S3_MANIFEST_BOTTLENECK.md)
- [Architecture Decision Records](design/adr/README.md)
- [CLI-First Rationale](design/CLI_FIRST_RATIONALE.md)
- [Typer Framework Decision](design/TYPER_RATIONALE.md)

## Project Goals

- **Cost Reduction**: Reduce backup costs from $1,700/month to ~$618/month (64% savings)
- **Reliability**: Automated testing with weekly/monthly/quarterly restore drills
- **Simplicity**: CLI-first design for technical users
- **Transparency**: Built-in cost tracking and reporting

## License

MIT
