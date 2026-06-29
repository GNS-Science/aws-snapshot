# ADR-014: Pre-inventory health-report signals (process layer + opt-out UX)

- Status: Proposed
- Date: 2026-06-27

## Context

[ADR-005](ADR-005-weekly-health-report.md) and
[ADR-009](ADR-009-health-check-measurement-model.md) designed the
daily health report around **inventory-driven signals**:
- inventory freshness (class-3 yellow when stale)
- source-vs-backup divergence (class-1 red on backup-missing-source-keys)
- day-over-day source-count delta (class-2 informational)
- daily restore-sample on the canary source (class-1 red on failure)

Each of these except restore-sample depends on the S3-Inventory +
Athena + Glue pipeline being deployed. Without it, the report renders
every source with `inventory_age=n/a`, three `NoSuchBucket` warning
lines per source, and an overall RED — every day, identically. The
signal-to-noise ratio is zero.

This bites two operator profiles:

1. **Small installs** where the inventory pipeline is operationally
   too heavy for the volume of objects. The cheapest inventory bucket
   + Athena workgroup is overhead disproportionate to a sub-30,000-
   object source.
2. **In-progress installs** that haven't deployed inventory yet.
   `public-record-backup` was here when its first natural daily report
   landed as RED-on-everything.

Both profiles need a report that conveys real health from the data
the engine already collects — `commands.status.get_status_dict`
returns per-source DynamoDB export status, S3 batch job histories,
and run-state records. That's enough to answer "did the backup
process actually run today, and did the work it did succeed?" —
which is what the operator wants the daily report to tell them.

ADR-009 already provided an `inventory_enabled: false` opt-out at the
`SourceConfig` level, but the report didn't change behaviour beyond
suppressing the inventory-driven red. The opt-out still surfaced a
daily `inventory_age=n/a` chip and a "inventory disabled for this
source — restore test is the dominant signal" info-note, restating
the same fact every day.

## Decision

Introduce a **process signals** layer alongside the existing
inventory-driven correctness signals, and make the opt-out genuinely
quiet:

1. **New `ProcessSignals` dataclass** on `SourceHealthData`:
   - `last_backup_at`, `last_backup_age_hours` — derived from S3
     batch run-state and DDB export timestamps
   - `last_s3_batch_jobs[]` — per-bucket batch summaries with
     `failure_pct`
   - `ddb_export_summary` — counts by status (completed / failed /
     in_progress / no_recent / errored)

2. **New extractor `_extract_process_signals(status_dict, now)`** —
   derives the fields from existing `get_status_dict` output. No new
   AWS calls.

3. **Classifier additions in `_classify_source`**:
   - **Red** if `last_backup_age > _BACKUP_AGE_RED_HOURS` (default 36)
   - **Red** if any `last_s3_batch_jobs[].failure_pct >
     _BATCH_FAILURE_RED_PCT` (default 0.10)
   - **Red** if any DDB export's most recent status is `FAILED`
   - **Yellow** if `last_backup_age > _BACKUP_AGE_YELLOW_HOURS`
     (default 12)
   - Existing inventory-class branches still apply; process signals
     layer additively.

4. **Renderers** (text / Slack / Discord) gain a `backup_age=N.Nh`
   chip in the per-source detail row alongside `inventory_age=` /
   `restore=`.

5. **Opt-out UX**: when `inventory_enabled: false`, formatters omit
   the `inventory_age` chip entirely and `build_report` no longer
   appends the "inventory disabled" info_note. The absence of
   inventory chips is itself the signal.

## Two-layer signal model

| Layer | Question | Source | Required infra |
|---|---|---|---|
| Process (new in this ADR) | Did the conveyor belt run? | `get_status_dict` | None |
| Correctness (existing) | Did the right boxes come out? | Athena queries over S3 Inventory | Inventory pipeline |

The layers answer different questions and don't conflict:
- Process GREEN + Correctness RED → "we ran but our outputs are wrong"
- Process RED + Correctness GREEN → "we missed a cycle but yesterday's
  state still verifies"
- Both GREEN → actually healthy
- Both RED → escalate

Operators with inventory deployed see both layers. Operators with
`inventory_enabled: false` see only the process layer (plus the
restore-test result on the canary source). Either is a useful daily
signal; the no-inventory case is no longer a wall of red.

## Alternatives considered

- **Require the inventory pipeline.** Rejected: operationally heavy
  for small installs; the cheapest viable Athena + Glue config still
  costs a few dollars/month + standing setup.
- **Drop correctness signals entirely and rely only on process +
  restore-test.** Rejected: process signals miss real failure modes
  (silent skipping of unreadable objects, mass deletions
  unintentionally mirrored to backup, retention-policy bugs). The
  inventory layer is the actual correctness verification when it's
  deployed.
- **Promote process signals to a separate report.** Rejected: same
  daily cadence, same data inputs, same audience. Splitting adds
  cognitive load without separation of concerns.

## Consequences

- The report is useful at every stage of an install's life: pre-
  inventory, post-inventory, opted-out.
- **Threshold defaults need calibration per install.** The chosen
  defaults (36h-red / 12h-yellow / 10%-red) were tuned against the
  `public-record-backup` baseline (daily schedule + the chronic 92%
  KMS-cross-account failure scenario from `public-record-backup-ops`
  issue #6). Installs on different cadences or scales may want to
  override via config — the thresholds should plausibly migrate to
  `HealthReportConfig` in a follow-up.
- The architectural question of what `last_run.status = "submitted"`
  means once the s3-backup Lambda has exited is **out of scope of
  this ADR**. Documented at issue
  [#66](https://github.com/GNS-Science/nzshm-backup/issues/66) and to
  be resolved by ADR-015 (forthcoming).
- The opt-out is now genuinely quiet rather than daily-noisy. Side
  effect: an operator who toggled `inventory_enabled: false` by
  mistake won't see a daily reminder of that decision. Acceptable
  trade-off; `make status` and `backup check` both still surface
  inventory state explicitly when queried.

## Implementation history

Already implemented and deployed pre-ADR (a process failure documented
in [#67](https://github.com/GNS-Science/nzshm-backup/issues/67)).
The merged code lives at:

- #63 `af44d35` — `ProcessSignals` dataclass, extractor, classifier
  branches, renderer chip
- #64 `70dad8d` — field-name fix for the extractor (typo in #63)
- #65 `ba9e6f9` — opt-out UX (chip + info-note suppression) and the
  `"submitted"` acceptlist change (the latter half is the scope of
  ADR-015 / #66, not this ADR)
