# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AWS-native backup CLI replacing AWS Backup for NSHM datasets. See `docs/design/backup-solution-plan.md` for full architecture, phase status, cost analysis, and design decisions.

## Commands

```bash
# Setup
uv sync --all-extras      # install all deps (replaces poetry install)

# Common workflows via Makefile
make test                 # run pytest
make lint                 # ruff + mypy
make fmt                  # ruff format + ruff --fix
make check                # lint then test
make upgrade              # upgrade deps with 1-week safety margin (--exclude-newer)

# Run individual tools directly
uv run pytest tests/test_foo.py::test_bar
uv run ruff check src/ tests/
uv run mypy src/

# Run CLI
uv run backup --help
uv run backup status                                       # all production sources
uv run backup run --source toshi --dry-run

# Notification + health-report ops (see docs/operations/cheatsheet.md)
uv run backup health-report preview                        # daily report dry-run
uv run backup notifications show                           # SNS subscription state
```

**Dependency upgrades:** always use `make upgrade` (or `uv lock --upgrade --exclude-newer <7-days-ago>`) to avoid picking up packages released in the last week.

## Architecture

**Package:** `src/aws_snapshot/` (src-layout, installed as `aws-snapshot` — renamed from `nzshm-backup` per migration issue #48)

**Entry point:** `src/aws_snapshot/cli.py`

**Key modules:**
- `commands/` — one file per subcommand group: `run_backup`, `status`, `schedule`,
  `config`, `restore`, `test`, `events`, `check`, `setup`, `report`, `costs`,
  `health_report`, `notifications`
- `backup_engine.py` — shared per-source backup logic (S3 + DynamoDB), used by CLI and Lambda
- `s3_backup.py` — S3 incremental sync, bucket lifecycle, cross-account session
- `dynamodb_backup.py` — PITR export initiation, export bucket setup
- `athena_inventory.py` — S3-Inventory-driven manifest build + per-partition COUNT(*) (count_delta)
- `inventory_state.py` — inventory freshness + bucket-pair health
- `health_report.py` — daily-report orchestrator (status + freshness + delta + sampled restore + PITR)
- `notifications/{slack,sns}.py` — transport modules used by health_report.send()
- `config/models.py` — Pydantic config schema
- `config/loader.py` — load from file, env var, or SSM Parameter Store
- `lambda_handler.py` — EventBridge Lambda entry point; branches on `task.task_type`
  (`backup` per-source or `health_report` daily report)
- `lambda_schema.py` — `BackupTask` Pydantic schema validated against EventBridge events
- `time_utils.py` — DST-aware NZ wall-clock helpers (Pacific/Auckland)

## Operational guidance

For day-to-day ops tasks (changing recipients, tuning thresholds, modifying
schedules, deploying code), the **single best entry point is
`docs/operations/cheatsheet.md`** — it maps "I want to change X" to the
exact command sequence. Other key references:

- `docs/user-guide/health-report.md` — daily health report walkthrough
- `docs/operations/enabling-notifications.md` — notification channel setup
- `docs/operations/inventory-bucket-recovery.md` — DR runbook for the
  control-plane bucket
- `docs/PROD-DEPLOY-LOG.md` — chronological deploy history

## Commit Style

Propose a commit after each logical unit of work is verified working. Check `git status` at session start to catch uncommitted drift.

## Code Style

- Line length: 100 (ruff)
- Target Python: 3.10+
- Type annotations expected (mypy configured)
- Ruff selects: E, F, W, I, N, UP, B, C4
- DRY — docs and tests must stay in sync with code
