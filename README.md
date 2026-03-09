# NSHM Backup Solution

AWS-native backup management CLI for NSHM datasets (ToshiAPI and THS).

## Features

- **Configuration Management**: YAML-based config with alias→ARN mapping
- **S3 Backup**: Incremental sync with lifecycle policies (Standard→Glacier→Deep Archive)
- **Automated Scheduling**: EventBridge rules for weekly/daily backups (via Serverless Framework)
- **Restore Operations**: Preview and execute restores with cost estimation
- **Automated Testing**: Weekly/monthly/quarterly restore validation
- **Cost Tracking**: Real-time cost monitoring and reporting
- **Notifications**: SES email and Slack integration

## Implementation Status

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 Step 1 | ✅ Complete | CLI skeleton with Typer |
| Phase 1 Step 2 | ✅ Complete | Config system + S3 backup operations |
| Phase 2 | 🔄 Coming Soon | DynamoDB export + EventBridge scheduling |
| Phase 3 | 🔄 Coming Soon | Notifications + cost reporting |
| Phase 4 | 🔄 Coming Soon | Restore functionality |
| Phase 5 | 🔄 Coming Soon | Testing + validation |

## Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e .
```

## Usage

```bash
# Show help
backup --help

# Configuration
backup config show                  # Show full configuration
backup config validate              # Validate config file
backup config show retention.days   # Show specific config key

# Run backup
backup run --source toshi           # Backup ToshiAPI source
backup run --source ths             # Backup THS source
backup run --all                    # Backup all sources
backup run --dry-run                # Preview backup without executing
backup run --full-sync              # Force full copy (not incremental)

# Status & reporting
backup status                       # Show backup status
backup costs report --period last-month
```

## Documentation

- [Design Plan](docs/backup-solution-plan.md) - Complete architecture and cost analysis
- [CLI Reference](docs/cli-reference.md) - Full command reference
- [Configuration](docs/getting-started/configuration.md) - Config file schema and examples

## Configuration

Copy `backup-config.example.yaml` to `backup-config.yaml` and customize:

```yaml
general:
  region: ap-southeast-2
  environment: production

sources:
  toshi:
    display_name: "ToshiAPI"
    s3_buckets:
      - arn:aws:s3:::ToshiAPI
    dynamodb_tables: []  # Phase 2

retention:
  hot_days: 30    # S3 Standard
  warm_days: 90   # Glacier Instant
  cold_days: 365  # Deep Archive
  max_age_days: 365
```

## Deployment

### Serverless Framework

```bash
# Install serverless
npm install -g serverless

# Deploy Lambda
serverless deploy

# Deploy with EventBridge schedules enabled
serverless deploy --stage prod
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/ tests/

# Lint
ruff check src/ tests/
```

## License

MIT
