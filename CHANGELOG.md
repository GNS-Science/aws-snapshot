# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Observability

- **Pre-inventory process signals in the daily health report.** The health report previously *required* the inventory pipeline (S3 Inventory + Athena + Glue) to render anything meaningful — installs that hadn't deployed inventory got the same `inventory_age=n/a` / `NoSuchBucket` walls every day. Now the report extracts pre-inventory process signals from the same `commands.status.get_status_dict` data it already collected, so a source can show "GREEN, last backup 2.1h ago, 31/31 DDB exports COMPLETED, 1000/1000 batch tasks succeeded" even without inventory.
  - New `ProcessSignals` dataclass on `SourceHealthData` carries last-backup timestamp, S3 batch job summaries, and DynamoDB export status counts per source.
  - `_extract_process_signals(status_dict, now)` derives the fields from existing status data — no extra AWS calls.
  - Classifier (`_classify_source`) gains process-signal branches:
    - **Red** if last backup > 36h, any recent batch job had > 10% failures, or any DDB export FAILED.
    - **Yellow** if last backup > 12h (early-warning lane).
    - **Green** otherwise; pairs naturally with `inventory_enabled: false` for sources that opt out of inventory entirely.
  - Renderers (Slack + Discord + email) show `backup_age=N.Nh` in the per-source detail row alongside `inventory_age=` and `restore=`.
  - Process notes (warnings + info) surface alongside the existing inventory notes so an operator can distinguish "process unhealthy" from "correctness unverified."
  - 8 new tests covering classifier branches and the extractor helper.

### Notifications

- **Discord support added** as a third notification channel alongside Slack and SNS. Uses Discord's native webhook format with rich embeds (color-coded by status, structured fields per source, footer with build metadata), distinct from Discord's Slack-compatibility `/slack` endpoint which is too restrictive for the engine's Block Kit payload (rejects `header` + `context` blocks).
  - New module `aws_snapshot.notifications.discord` with `send_discord(webhook_url, embeds, content)`.
  - New `DiscordConfig` model under `notifications.discord` (mirrors `SlackConfig`).
  - `health_report.send()` now delivers to both Slack and Discord independently when each is enabled.
  - `lambda_alarm_bridge.handler` posts to Discord when `DISCORD_WEBHOOK_SECRET_ID` env var is set, otherwise Slack (preserving default for existing installs).
  - SAM template gains `DiscordWebhookSecretName` parameter (default empty = Slack-only).
  - Webhook URLs go in Secrets Manager as the standard form `https://discord.com/api/webhooks/{id}/{token}` — **no `/slack` suffix** (that's the Slack-compat endpoint, which we don't use).

### Deploy mechanism

- **No Node.js dependency.** Removing the Serverless Framework toolchain takes Node out of the development and deploy story entirely. New operators no longer install Node, run `npm install`, manage `serverless-python-requirements` plugin state, or chase `~/Library/Caches/serverless-python-requirements/` cache bugs. Onboarding shrinks to one command: `make sync`. CI gets faster (no `npm ci` step, no `node_modules` cache to warm). Dependabot's npm noise — the bulk of recent open alerts came in via Serverless transitive `axios` / `form-data` / etc. — stops permanently. Repo size on fresh clones drops because the `~250 MB` transitive npm dep tree is no longer pulled.

- **AWS SAM replaces Serverless Framework as the production deploy mechanism.** Translated `serverless.yml` to `sam.yaml`; introduced a Makefile-driven SAM build that produces clean ~55 MB Lambda artefacts (vs the bloated builds the default SAM Python builder would have produced); side-stack-verified 2026-06-23; cut over in production 2026-06-24. The legacy npm-based Serverless Framework toolchain (`serverless.yml`, `package.json`, `package-lock.json`, `node_modules/`) is removed in this release. SAM CLI is now a declared dev dependency in `pyproject.toml`. Issue #48, PR #51 (cutover) + this PR (`serverless.yml` removal).

- `template.yaml` renamed to `sam.yaml`; `samconfig.toml` gains `template_file = "sam.yaml"` for SAM CLI discovery transparency. (Per @voj review feedback.)

### Internal

- `docs/PROD-DEPLOY-LOG.md` retired in the public repo; authoritative copy now lives in the private `nzshm-backup-ops` shim repo per the shim-strategy decision in PR #49. A redirect note remains at the old path.
- `backup-config.production.yaml` removed from the public repo; authoritative copy now lives in `nzshm-backup-ops` (same rationale as PROD-DEPLOY-LOG). Added to `.gitignore` to prevent accidental re-tracking. `backup-config.example.yaml` is retained as the OSS-facing template.

### Other

- deps: patch (12 pkgs), minor (22 pkgs), major: cryptography 47→48, mypy 1→2

## [v0.1.0] - 2026-06-12

First tagged release. Captures all work landed on `pre-release` since
the project began — covering Phases 1–7 of the implementation plan
(backup, schedule, restore, notifications, daily health report,
ADR-009 signal-class taxonomy with head-check tagging, ADR-006
two-tier lifecycle migration). Production has been running this code
since at least 2026-05-22 (see PROD-DEPLOY-LOG Steps 14–22 for the
deploy chronology and validation evidence).

### Changed (breaking — config schema)

- **Daily health report — class-1 backup-missing-source-keys signal + class-2
  reclassification of source-count delta** (ADR-009 / #23). Single Athena
  `FULL OUTER JOIN` query (`divergence_counts`) returns both directions of
  source-vs-backup divergence in one scan. `source_minus_backup > 0` is now
  the class-1 red signal that means the backup system has actually failed;
  `backup_minus_source > 0` is class-2 informational (orphans from
  source-side deletions retained per ADR-006). The previous day-over-day
  source-count delta is reclassified from red to class-2 informational,
  and the `delta_pct_threshold` / `delta_abs_threshold` keys are removed
  from `HealthReportConfig` and the production YAML — they no longer apply.
  Report layout grows distinct `⚠` (warnings) and `ℹ` (informational)
  sub-lines per source row.
- **`SourceConfig.inventory_enabled` (default true)** lets a source opt
  out of the S3 Inventory + Athena pipeline. When false the daily report
  skips inventory-age, divergence, and count-delta for that source and
  the classifier no longer reds on missing inventory data — restore test
  becomes the dominant red signal. Used by the sandbox validation
  runbook to exercise the "no Inventory" floor; also a fit for small
  config buckets where the daily Athena cost isn't worth standing up.
  A Pydantic `@model_validator` rejects the inconsistent combination
  `inventory_enabled: false` + `batch_manifest_mode: inventory` at
  config-load time — the Batch path needs Inventory to build its
  manifest, so the opt-out flag must come with `batch_manifest_mode:
  inline` (or `use_s3_batch: false`).
- **New runbook**: `docs/operations/purge-from-backup.md` — out-of-band
  procedure for removing class-2 orphans when retention is no longer
  desired. Small-list (`aws s3api delete-objects`) and large-list
  (S3 Batch with version-scoped manifest) paths; #24 is a candidate
  large-list use case.
- **Simplify backup-bucket lifecycle to two tiers, no expiry** (ADR-006 / #17).
  The lifecycle policy now has a single Standard → Glacier Instant Retrieval
  transition at `hot_days` (default 30) and backup objects are retained
  forever. This removes the silent annual re-copy at the 365-day expiration
  boundary and eliminates the need for the unimplemented Deep Archive thaw
  flow. DR retrieval drops from 12–48h to milliseconds; cost rises from
  ~$47/mo to ~$108/mo (still ~16× cheaper than AWS Backup).
- **Removed retention config keys**: `warm_days`, `cold_days`, and
  `max_age_days` no longer exist on `RetentionConfig` or `LifecycleConfig`.
  Remove them from any `backup-config.*.yaml`. ADR-006 mitigations
  (object-count delta health signal, manual-purge runbook) have been
  implemented under ADR-009 / #23 (see above).

### Fixed

- **Class-1 RED divergence note tagged with live-state head-check** (ADR-009).
  When `divergence_counts` reports `source_minus_backup > 0`, the report
  now samples up to 10 missing keys via `athena_inventory.divergence_sample_keys`
  and `head_object`-checks each against the live backup bucket. The existing
  RED note gains a tag:
  - All sampled keys 404 → `"(still missing live, sampled N)"` — the gap
    is current; backup hasn't re-synced yet.
  - All sampled keys 200 → `"(auto-healed since snapshot, sampled N)"` —
    the inventory snapshot captured a gap that a subsequent backup run
    has already repaired. Row stays RED (audit framing preserved per
    ADR-009 — the gap existed at snapshot time and deserves operator
    attention regardless).
  - Mixed → `"(X still missing, Y auto-healed, sampled N)"`.
  Closes the operator-experience gap surfaced by the toy-inv sandbox
  Cycle-1 validation: the daily report could fire RED at 10:45 NZT for
  divergences that the 09:45 NZT backup had already self-healed,
  leaving operators chasing non-issues. A failed sample query falls
  back to the original untagged note shape + a separate
  `"head-check sample failed: ..."` diagnostic note. Only the class-1
  (source-minus-backup) direction is verified; class-2 orphans remain
  count-only.
- **`_latest_object_ts` skips 0-byte placeholder objects** in the inventory
  freshness check. Setup-inventory (or AWS itself when wiring up the
  InventoryConfiguration) can leave a 0-byte folder marker at the
  destination prefix before any real inventory data has been delivered.
  Previously its LastModified counted as a valid freshness signal,
  silently masking the class-1 "no inventory data available" red — a
  newly-configured source appeared GREEN until first real delivery (~18 h
  later). Caught by the toy-inv sandbox source on 2026-05-27: today's
  15:45 NZT report came back 6/6 GREEN when toy-inv should have been
  RED. Real inventory artifacts (manifest.json / manifest.checksum /
  parquet) are always >0 bytes, so the size filter doesn't change
  behaviour for healthy production sources.
- **`backup config push` auto-upgrades to SSM Advanced tier when needed.**
  The serialised config blob exceeded the 4 KB Standard-tier limit once
  two toy sources were added for ADR-009 sandbox validation; push would
  fail with `ValidationException`. Now selects `Tier="Advanced"` when
  the payload is > 4 KB and reports the chosen tier in the success
  message. Advanced costs ~$0.05/parameter/month; upgrade is one-way
  per parameter.
- **`load_dotenv()` now runs at CLI module-import time**, not inside
  the `@app.callback`. The previous ordering meant `BACKUP_CONFIG_PATH`
  from `.env` was not visible to typer `Option(os.getenv(...))`
  defaults — those evaluate at function-definition time during
  subcommand import, which happens *before* `@app.callback` runs.
  Operators were forced to pass `--config` explicitly on every
  command. Caught during ADR-009 sandbox setup walkthrough.
- **Lambda IAM: scoped `s3:DeleteObject` / `s3:DeleteBucket` for restore-test
  temp buckets** (2026-05-22). The role's deliberate "no delete on backup
  buckets" stance meant the daily health-report Lambda silently failed to
  clean up the temp buckets it created during sampled restore tests, leaving
  one bucket per restored source per day. Fix: name-pattern-scoped Allow on
  the `bb-restore-test-*` prefix only; the no-delete guarantee on real
  backup buckets (`bb-<source>-*`) is preserved. Two orphans from the
  2026-05-22 02:30 UTC fire were cleaned up with admin credentials before
  redeploy.

### Added

- **`backup setup lifecycle` command** (#27). Walks the configured S3 and
  DynamoDB backup buckets for the selected source(s) and re-applies the
  lifecycle policy derived from `config.retention`. Required because
  `apply_lifecycle_policy` only runs at bucket creation, so a change to
  `RetentionConfig` (e.g. ADR-006 dropping Deep Archive) does not
  propagate to already-deployed buckets via `backup run`. Supports
  `--source <alias|all>` and `--dry-run`. Used post-#25 merge to migrate
  all five production buckets to the new two-tier policy in a single
  command.
- **New user-guide page**: `docs/user-guide/setup.md` — reference for
  the four `backup setup` subcommands (`iam source-roles`, `iam
  backup-batch-role`, `inventory`, `lifecycle`), with a first-time
  deploy ordering recipe. Previously the `setup` namespace had no
  dedicated docs page.
- **Lambda-error alarm fast path** (ADR-005 / #16). CloudWatch alarm on the backup
  Lambda's `Errors` metric (≥ 1 over 5 min) → SNS topic → email subscription. Routes
  to `notifications.alerts.email` from `backup-config.{stage}.yaml`. Sandbox stage
  skips the email subscription via a CloudFormation Condition.
- **`backup test alert` command** — forces the alarm into `ALARM` state without a
  real Lambda failure, so the SNS → email path can be exercised after deploy.
  Auto-returns to OK on the next real datapoint (~5 min) with an OK notification.
- New `AlertsConfig` Pydantic model (`notifications.alerts.email`) distinct from
  SES recipients (slow-path daily report) and Slack (ADR-005, future).
- **Daily health-report slow path** (ADR-005 / #16). `health_report.py` orchestrator
  combines `get_status_dict` (per-source state), `restore_test_source` (weka canary
  daily + Mon/Wed/Fri rotation through ths/toshi/static), inventory freshness check
  (>30h → yellow), and the new `athena_inventory.count_delta` (>=5% drop or >=10k
  absolute → red). Delivers via Slack Block Kit webhook **and** plain-text email
  through a separate `nzshm-backup-reports-{stage}` SNS topic. Configurable via
  `notifications.slack.enabled` and `notifications.reports.email.{enabled,address}`;
  ships disabled — see `docs/operations/enabling-notifications.md` for turn-on
  procedure. Lambda picks up the topic ARN via `$BACKUP_REPORTS_TOPIC_ARN`.
- **`backup health-report run|preview`** CLI for exercising the slow path locally
  (with prod credentials). The scheduled Lambda dispatch path lands in the same
  release (see *Daily health-report trigger* entry below).
- Reusable programmatic APIs: `commands.status.get_status_dict` extracted from
  `_print_json_status`; `commands.test.restore_test_source` extracted from the
  `backup test restore` CLI as a pure `RestoreTestResult`-returning function.
- `notifications/slack.py` and `notifications/sns.py` thin transport modules with
  Secrets Manager retrieval, Subject-length truncation, and structured error types.
- `time_utils.nz_now()` and `time_utils.nz_today()` — DST-aware NZ wall-clock
  helpers (via `zoneinfo.ZoneInfo("Pacific/Auckland")`). Used by the daily report
  so report_date and weekday rotation reflect NZ calendar, not UTC.
- **Multi-recipient notification subscriptions managed from YAML.**
  `notifications.alerts.emails` and `notifications.reports.email.addresses`
  are now lists of strings (was: singular `email` / `address`).
  New `backup notifications apply` command reconciles each SNS topic's
  email subscriptions to match the YAML lists — `+` Subscribe for new
  addresses, `-` Unsubscribe for removed, leaves pending confirmations
  alone. `backup notifications show` lists current state.
  `serverless.yml` no longer manages individual subscriptions (the
  topics themselves are still CloudFormation-owned); recipient changes
  no longer require `sls deploy`.
- **Daily health-report trigger** (ADR-005 / #16; Lambda dispatch + EventBridge schedule).
  `BackupTask.task_type: Literal["backup","health_report"] = "backup"` discriminates
  Lambda invocations. New handler branch calls `health_report.build_report` +
  `send` when `task_type == "health_report"`, then appends a `health_report_run`
  event to the canary's backup bucket. The `backup schedule add/remove/enable/disable`
  CLI now accepts `--task-type health_report` — health-report rules use the fixed
  name `nzshm-backup-health-report-{frequency}` and carry the task_type in their
  EventBridge target Input. Operator deploy:
  ```
  backup schedule add --source _health --task-type health_report \
      --frequency daily --time 14:30-NZST
  ```

### Changed

- **Migrated from Poetry to uv** — build backend switched from `poetry-core` to
  `hatchling`, dev dependencies moved to `[dependency-groups]` (PEP 735), lockfile
  is now `uv.lock`.
- **Replaced black with ruff format** — single tool for both formatting and linting.
- Added tox configuration (`setup.cfg`) with `py310`/`py311`/`py312`, `format`,
  `lint`, `build-linux`/`build-macos`, and `audit` environments.
- Added GitHub Actions CI workflow (`.github/workflows/dev.yml`) using
  `GNS-Science/nshm-github-actions/.github/workflows/python-run-tests-uv.yml`.
- Added `tox`, `tox-uv`, and `pip-audit` to dev dependency group.

### Fixed

- Tests in `test_schedule.py`, `test_cli.py`, and `test_lambda_handler.py` now set
  `AWS_DEFAULT_REGION` — previously failed with `NoRegionError` when `boto3.Session()`
  was created without an explicit region.
- Mypy errors in `inventory_state.py` (no-any-return) and `schedule.py`
  (incompatible type assignment) resolved.

---

## Previous (pre-migration)

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
- `backup test restore` now samples objects via Athena inventory query (`ORDER BY
  RAND() LIMIT N`) instead of listing the entire backup bucket. Falls back to
  full listing when inventory is unavailable. THS (3.8M objects) sampling now
  completes in seconds instead of minutes.
- `backup test restore` now verifies restored objects using S3 checksums
  (CRC64NVME/CRC32/SHA256 via `GetObjectAttributes`) when available, falling
  back to ETag comparison when not. Checksums are content-deterministic
  regardless of upload method, eliminating false ETag mismatches.

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

- `test restore` now refuses to fall back to full bucket listing for
  inventory-mode sources when inventory is unavailable. Prints an actionable
  message instead of silently stalling for hours on multi-million-object buckets.
- `test integrity` now warns before running full listing on inventory-mode
  sources with potentially millions of objects.

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
- Inventory diff now uses smart ETag comparison: only compares ETags when both
  source and backup are single-part uploads (no `-N` suffix). Multipart ETags
  are not content-deterministic (they depend on upload chunk boundaries), so
  the diff falls back to size-only for those keys. This eliminated 4,224 false
  positives per THS run caused by S3 Batch copy producing different ETags for
  identical content. Inventory table schema now includes `checksum_algorithm`
  column to support future SHA256 content checksum comparison when enabled.
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
