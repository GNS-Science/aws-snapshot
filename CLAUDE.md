# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AWS-native backup CLI replacing AWS Backup (~$1,700 NZD/month) for NSHM datasets (ToshiAPI DynamoDB tables + S3 buckets). Target: ~$618 NZD/month via S3 Glacier lifecycle policies and DynamoDB Point-in-Time exports.

The CLI is installed as the `backup` command and is intended to run on AWS Lambda triggered by EventBridge.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e .          # production deps
pip install -e ".[dev]"   # dev deps (pytest, black, ruff, mypy)

# Run tests
pytest                    # all tests with coverage
pytest tests/test_foo.py::test_bar  # single test

# Lint & format
ruff check src/ tests/
black src/ tests/
mypy src/

# Run CLI
backup --help
backup status
backup run --source toshi --dry-run
```

## Architecture

**Package:** `src/nzshm_backup/` (src-layout, installed as `nzshm-backup`)

**Entry point:** `src/nzshm_backup/cli.py` — creates the root `app = typer.Typer()` and registers all subcommand groups via `app.add_typer(...)`.

**Command modules** in `src/nzshm_backup/commands/`:
- `schedule.py` — show/set/enable/disable backup schedules (EventBridge)
- `run_backup.py` — manual backup trigger for `toshi`, `ths`, or `all`
- `restore.py` — list/preview/run/cancel restores with cost estimation
- `test.py` — automated restore validation and integrity checks
- `status.py` — current backup state, last/next run
- `report.py` — backup activity reports
- `costs.py` — cost tracking, projection, and export
- `config.py` — read/write `backup-config.yaml` settings

**Data sources:**
- `toshi`: ToshiAPI S3 bucket (~8 TB) + DynamoDB FileTable (2.3 GB) + ThingTable (16 GB)
- `ths`: THS_dataset_prod S3 bucket (~1 TB)

**Storage tiers:** S3 Standard (0-30 days) → S3 Glacier Instant (31-90 days) → S3 Glacier Deep Archive (91-365 days) → delete

**Configuration:** `backup-config.yaml` (YAML, version-controlled); loaded by `config` command. See `docs/backup-solution-plan.md` for full schema.

## Implementation Status

Currently Phase 1 (CLI skeleton). All command handlers return "coming soon" stubs. No tests exist yet. The `tests/` directory is empty.

## Code Style

- Line length: 100 (black + ruff)
- Target Python: 3.10+
- Type annotations expected (mypy configured)
- Ruff selects: E, F, W, I, N, UP, B, C4
- DRY
- Docs need to stay in sync with code / tests

## Key Design Decisions

- **Typer** chosen over Click for automatic `--help` generation and type-safe options (see `docs/TYPER_RATIONALE.md`)
- All destructive operations must support `--dry-run`
- JSON output (`--output json`) supported for scripting
- Restore cost approval: auto-approve <$100 NZD, email approval $100-500, dual approval >$500
- DynamoDB restores always go to a new table (never overwrite in-place)
- Temporary restore buckets auto-delete after 7 days: pattern `nzshm-restore-{source}-{date}-{random}`
