# Installation

## Prerequisites

- Python 3.10 or higher
- Poetry (dependency management)
- AWS credentials configured

## Install Poetry

If you don't have Poetry installed:

```bash
# macOS/Linux
curl -sSL https://install.python-poetry.org | python3 -

# Windows
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | python -
```

## Install from Source

```bash
# Clone the repository
git clone https://github.com/gns-science/nzshm-backup.git
cd nzshm-backup

# Install dependencies
poetry install

# Activate virtual environment
poetry shell
```

## Verify Installation

```bash
# Check CLI is available
backup --help

# Check version
backup --version
```

## AWS Configuration

Configure AWS credentials:

```bash
# Using AWS CLI
aws configure

# Or set environment variables
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=ap-southeast-2
```

## Shell Completion

Install shell completion for better CLI experience:

```bash
# Bash
backup --install-completion bash

# Zsh
backup --install-completion zsh

# Fish
backup --install-completion fish
```

## Installation Verification

Run a test command to verify everything works:

```bash
# Should show "coming soon" message
backup status

# Dry-run a backup
backup run --source toshi --dry-run
```

## Next Steps

- [Quick Start Guide](quickstart.md) - Learn basic commands
- [Configuration](configuration.md) - Set up your backup config
- [CLI Reference](../cli-reference.md) - Complete command documentation
