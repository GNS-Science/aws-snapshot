# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AWS-native backup CLI replacing AWS Backup for NSHM datasets. See `docs/backup-solution-plan.md` for full architecture, phase status, cost analysis, and design decisions.

## Commands

```bash
# Setup
poetry install            # all deps including dev

# Run tests
poetry run pytest
poetry run pytest tests/test_foo.py::test_bar

# Lint & format
poetry run ruff check src/ tests/
poetry run black src/ tests/
poetry run mypy src/

# Run CLI
poetry run backup --help
poetry run backup status --source arkivalist
poetry run backup run --source arkivalist --dry-run
```

## Architecture

**Package:** `src/nzshm_backup/` (src-layout, installed as `nzshm-backup`)

**Entry point:** `src/nzshm_backup/cli.py`

**Key modules:**
- `commands/` — one file per subcommand group (run_backup, status, schedule, config, restore, …)
- `backup_engine.py` — shared per-source backup logic (S3 + DynamoDB), used by CLI and Lambda
- `s3_backup.py` — S3 incremental sync, bucket lifecycle, cross-account session
- `dynamodb_backup.py` — PITR export initiation, export bucket setup
- `config/models.py` — Pydantic config schema
- `config/loader.py` — load from file, env var, or SSM Parameter Store
- `lambda_handler.py` — EventBridge Lambda entry point

## Commit Style

Propose a commit after each logical unit of work is verified working. Check `git status` at session start to catch uncommitted drift.

## Code Style

- Line length: 100 (black + ruff)
- Target Python: 3.10+
- Type annotations expected (mypy configured)
- Ruff selects: E, F, W, I, N, UP, B, C4
- DRY — docs and tests must stay in sync with code
