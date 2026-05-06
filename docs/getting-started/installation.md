# Installation

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) (dependency management)
- AWS credentials configured

## Install uv

If you don't have uv installed:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Install from Source

```bash
# Clone the repository
git clone https://github.com/gns-science/nzshm-backup.git
cd nzshm-backup

# Install dependencies
uv sync --all-extras
```

## Verify Installation

```bash
# Check CLI is available
uv run backup --help

# Check version
uv run backup --version
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
uv run backup --install-completion bash

# Zsh
uv run backup --install-completion zsh

# Fish
uv run backup --install-completion fish
```

## Installation Verification

Run a test command to verify everything works:

```bash
# Should show "coming soon" message
uv run backup status

# Dry-run a backup
uv run backup run --source toshi --dry-run
```

## Next Steps

- [Quick Start Guide](quickstart.md) - Learn basic commands
- [Configuration](configuration.md) - Set up your backup config
- [CLI Reference](../cli-reference.md) - Complete command documentation
