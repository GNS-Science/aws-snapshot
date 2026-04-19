# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Added

- `backup schedule add` now supports `--target codebuild` for EventBridge -> CodeBuild
  schedules. This mode requires `--codebuild-project-arn` and `--target-role-arn`.
- `backup check [--source SOURCE]` — fast pre-flight command that validates IAM credentials,
  cross-account role assumption, S3 bucket read access, backup bucket existence, S3 Batch
  role presence, and DynamoDB PITR status. No object enumeration — completes in seconds.

### Changed

- `backup schedule add` now replaces existing EventBridge rule targets before
  registering a new target, preventing dual Lambda+CodeBuild triggering.
- `backup schedule remove` now removes all rule targets (not only `backup-lambda`)
  before deleting the rule.
- `backup schedule show` now displays rule target mode/details (`lambda`,
  `codebuild`, `mixed`, `none`) and JSON output includes enriched target metadata
  (`backup --output json schedule show`).

### Docs

- Updated scheduling docs with CodeBuild-target examples and a mixed-target
  release checklist for Lambda + CodeBuild operations.

### Fixed

- `batch_backup_source()` dry-run no longer enumerates all source objects. Previously a
  dry-run on an 8M-object bucket would paginate through ~80k ListObjectsV2 pages (10–20 min)
  even though the real run delegates listing to AWS S3 Batch. The dry-run fast-path now does
  a single `list_objects_v2(MaxKeys=1)` access check and returns immediately.
  `objects_in_manifest` is set to `-1` (not enumerated) instead of a count.
- `run_backup.py`: dry-run output for Batch sources now says "Would submit S3 Batch job"
  rather than displaying a stale manifest count.

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
