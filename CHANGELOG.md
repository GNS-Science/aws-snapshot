# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Fixed

- `backup config` subcommands (`show`, `push`, `pull`, `validate`) now honour the
  `BACKUP_CONFIG_PATH` environment variable. Previously `_get_config_path()` in
  `commands/config.py` only checked `state.config_path` (never set by the CLI) and fell
  through to the hardcoded default `backup-config.yaml`, silently ignoring the documented
  env var. Resolution order now matches `load_config()` in `config/loader.py`:
  `state.config_path` → `BACKUP_CONFIG_PATH` → `./backup-config.yaml`.

### Changed

- `serverless.yml`: updated `org` to `gnssciencenshm`, added `app: nzshm-backup`, renamed
  `service` to `nzshm-backup-service`, added `deploymentPrefix: nzshm-backup`.

### Scripts

- `scripts/create-source-roles.py`: `--backup-account-id` can now be passed alongside
  `--config/--source` to override the backup account ID when `general.lambda_arn` is not
  yet set (e.g. before first Lambda deploy).
- `scripts/create-source-roles.py`: fixed dry-run crash — `_create_or_update_role()` was
  calling bare `boto3.client("sts")` in dry-run mode, ignoring the `--profile` flag and
  failing when env credentials were for a different account. `account_id` is now passed in
  from the already-resolved `sts.get_caller_identity()` call in `main()`.
