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

# Show backup status
backup status

# Run manual backup
backup run --source toshi

# Preview restore operation
backup restore preview --date 2026-02-15 --source toshi

# Show schedule
backup schedule show

# Generate cost report
backup costs report --period last-month
```

## Documentation

See [docs/backup-solution-plan.md](docs/backup-solution-plan.md) for complete design documentation.

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
