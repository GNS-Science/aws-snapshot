# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Added

- `backup schedule add` now supports `--target codebuild` for EventBridge -> CodeBuild
  schedules. This mode requires `--codebuild-project-arn` and `--target-role-arn`.
- New `backup setup` command group for provisioning workflows:
  - `backup setup inventory ...`
  - `backup setup iam source-roles ...`
  - `backup setup iam backup-batch-role ...`
- `backup check [--source SOURCE]` — fast pre-flight command that validates IAM credentials,
  cross-account role assumption, S3 bucket read access, backup bucket existence, S3 Batch
  role presence, and DynamoDB PITR status. No object enumeration — completes in seconds.

### Changed

- **Inventory manifest generation replaced with Athena UNLOAD pipeline.**
  Previously, Athena query results were streamed through Lambda to build the
  S3 Batch manifest CSV. This OOM'd at 1024 MB and would take ~8 hours for
  40M-object sources. The new approach uses Athena `UNLOAD` to write manifest
  CSV directly to S3 (server-side), with URL encoding via a SQL `REPLACE()`
  chain. Lambda only orchestrates — no data flows through its memory. The
  `static` source (39.9M objects) now completes manifest generation in ~28
  seconds at 432 MB peak memory.
- All production sources (toshi, ths, static, weka) now run on Lambda via the
  inventory-based Athena UNLOAD pipeline. CodeBuild is retained as a fallback
  but no longer required.
- `backup schedule add` now replaces existing EventBridge rule targets before
  registering a new target, preventing dual Lambda+CodeBuild triggering.
- `backup schedule remove` now removes all rule targets (not only `backup-lambda`)
  before deleting the rule.
- `backup schedule show` now displays rule target mode/details (`lambda`,
  `codebuild`, `mixed`, `none`) and JSON output includes enriched target metadata
  (`backup --output json schedule show`).
- `backup check` now reports inventory readiness signals (source/backup inventory
  config state and latest snapshot timestamps).
- `backup status` now includes inventory snapshot metadata in JSON output for
  S3 Batch sources.
- S3 Batch manifest preparation now supports per-source mode selection via
  `sources.<alias>.batch_manifest_mode`:
  - `inline` (default): live source+backup listing diff
  - `inventory`: diff from latest source/backup S3 Inventory snapshots via
    Athena queries in the control bucket
- S3 Batch role/source policy helper scripts now grant the full read/write action
  set required by copy jobs on large sources (`GetObject*`/version-tag variants,
  plus backup write ACL/tagging actions).

### Docs

- Updated scheduling docs with CodeBuild-target examples and a mixed-target
  release checklist for Lambda + CodeBuild operations.
- Added Athena manifest pipeline design doc and documented production finding
  that S3 Select on inventory Parquet returns `MethodNotAllowed`, so inventory
  diff implementation pivots to Athena.

### Fixed

- Lambda role in `serverless.yml` now has full Glue Data Catalog permissions
  (database, table, and partition CRUD) required by Athena inventory-diff queries.
  Previously only read actions (`GetDatabase`, `GetTable`, `GetTables`) were granted,
  causing scheduled toshi Lambda runs to fail with `AccessDeniedException` on
  `glue:CreateDatabase`, `glue:BatchCreatePartition`, and `glue:GetPartition`.
- Backup engine now writes `status="failed"` when S3 backup throws an exception.
  Previously the run state was left permanently stuck at `"running"` because the
  exception handler logged the error but never updated the state record.
- Athena inventory diff queries now accept `NULL` `is_latest`/`is_delete_marker`
  fields for non-versioned S3 buckets (e.g. `static`). Previously these rows
  were silently filtered out, producing empty manifests.
- Athena UNLOAD output now uses `compression = 'NONE'`. Default gzip compression
  produced binary manifests that S3 Batch could not parse.
- UNLOAD cleanup now deletes all objects under the intermediate prefix including
  0-byte `_SUCCESS` markers, preventing `HIVE_PATH_ALREADY_EXISTS` on retry.
- Empty backup inventory (first-ever backup) no longer crashes the inventory
  diff. The code falls back to a source-only query that copies everything.
- UNLOAD SQL REPLACE chain expanded from 9 characters to the full 28-character
  set that `urllib.parse.quote(key, safe='/')` encodes. The previous subset
  missed `+` (caused 2/39.9M static failures) and 18 other RFC 3986 reserved
  characters that could appear in S3 keys.
- Lambda IAM: added `s3:CreateJob`/`s3:DescribeJob`/`s3:ListJobs` alongside
  `s3control:` variants — the error message referenced the `s3:` prefix.
- S3 Batch manifest keys are now URL-encoded when generated, matching S3 Batch
  CSV requirements for object keys containing reserved characters (`=`, `(`, `)` etc.).
  This fixes THS copy failures that previously returned `403 AccessDenied` for encoded-key
  rows even when bucket policies were present.
- `backup run` now passes source alias and configured batch manifest mode into
  S3 Batch manifest prep, enabling inventory-mode manifests without changing
  the operator/scheduler command surface.
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

- Added `scripts/setup-inventory.py` to configure daily Parquet S3 inventory for
  source + backup buckets, with output to a dedicated control bucket.
- `scripts/create-source-roles.py`: `--backup-account-id` can now be passed alongside
  `--config/--source` to override the backup account ID when `general.lambda_arn` is not
  yet set (e.g. before first Lambda deploy).
- `scripts/create-source-roles.py`: fixed dry-run crash — `_create_or_update_role()` was
  calling bare `boto3.client("sts")` in dry-run mode, ignoring the `--profile` flag and
  failing when env credentials were for a different account. `account_id` is now passed in
  from the already-resolved `sts.get_caller_identity()` call in `main()`.
