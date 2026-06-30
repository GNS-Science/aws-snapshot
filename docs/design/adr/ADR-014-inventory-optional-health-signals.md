# ADR-014: Inventory-optional health signals (process layer)

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

Each of these except restore-sample depends on the S3 Inventory +
Athena + Glue pipeline being deployed.

### "Inventory-optional" is about scale, not time

The phrase that surfaced this gap during the `public-record-backup`
rollout was "pre-inventory installs" — but that framing implies a
temporal sequence ("we'll add inventory later"). In practice the
right framing is **scale**: many installs never reach a size where
S3 Inventory is the appropriate verification mechanism, and for
those installs the inventory pipeline is a permanent operational
cost without a proportional benefit.

S3 Inventory is designed for large buckets where listing the entire
contents (via `ListObjectsV2`) becomes prohibitively expensive. At
the scale where it's the right tool, Inventory delivers value:
daily CSV reports of every object's key, size, ETag, encryption,
storage class. [ADR-002](ADR-002-inventory-manifest-pipeline-ths.md)
established the engine's Inventory-driven manifest pipeline for
exactly this case (THS at millions of objects). Below that scale,
the same information is cheap to obtain ad-hoc and the daily
Inventory delivery — which the operator does not control the
timing of — becomes overhead.

Two operator profiles fall on the inventory-optional side of this
line:

1. **Small-scale installs.** Sub-30,000-object sources where the
   inventory pipeline is operationally too heavy for the volume.
   The cheapest viable inventory bucket + Athena workgroup + Glue
   tables is overhead disproportionate to what's being verified.
   These installs may remain inventory-optional permanently.
2. **Installs not yet at inventory scale.** Installs that may
   eventually deploy inventory but haven't crossed the scale or
   complexity threshold yet. `public-record-backup` was here when
   its first natural daily report landed as RED-on-everything,
   and may or may not cross the threshold later.

The pre-this-ADR report only had useful output for installs **on
the inventory side of the line**. Below that line, the report
rendered every source with `inventory_age=n/a`, three `NoSuchBucket`
warning lines per source, and an overall RED — every day,
identically. The signal-to-noise ratio was zero.

### Why inventory-optional is often the preferred state

For installs at a scale where ad-hoc verification is tractable,
remaining inventory-optional has positive properties beyond cost:

- **Simplicity.** No additional S3 buckets, no Athena workgroup, no
  Glue catalog, no Inventory delivery permissions to manage. The
  failure surface of the verification path shrinks.
- **Operator-controlled timing.** S3 Inventory is delivered on
  AWS's daily cadence — the operator does not pick the time, and a
  late delivery silently makes the day's report less informative.
  Process signals are computed at report-build time from live API
  state and have no such dependency.
- **Fewer cross-account permission moving parts.** Inventory adds
  cross-account delivery permissions, KMS keys for the inventory
  bucket, and Athena workgroup access policies. Inventory-optional
  installs avoid all of that.

The decision to deploy inventory should be driven by scale (object
counts approaching or exceeding `ListObjectsV2`'s ergonomics) or by
the need for the specific correctness checks inventory enables
(divergence detection at scale, day-over-day count tracking) — not
by a default expectation that inventory is always the right answer.

### What the report needs to do for inventory-optional installs

Both inventory-optional profiles need a report that conveys real
health from the data the engine already collects.
`commands.status.get_status_dict` returns per-source DynamoDB
export status, S3 batch job histories, and run-state records.
That's enough to answer "did the backup process actually run today,
and did the work it did succeed?" — which is what the operator wants
the daily report to tell them.

ADR-009 already provided an `inventory_enabled: false` opt-out at
the `SourceConfig` level, but the report didn't change behaviour
beyond suppressing the inventory-driven red. The opt-out still
surfaced a daily `inventory_age=n/a` chip and a "inventory disabled
for this source — restore test is the dominant signal" info-note,
restating the same fact every day.

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
signal; the inventory-optional case is no longer a wall of red and
is, for many installs, the preferred steady state.

## Alternatives considered

- **Require the inventory pipeline.** Rejected: as covered in the
  context, S3 Inventory is a scale-appropriate verification
  mechanism, not a universal one. Mandating it would impose cost and
  complexity on installs that don't benefit.
- **Drop correctness signals entirely and rely only on process +
  restore-test.** Rejected: process signals miss real failure modes
  (silent skipping of unreadable objects, mass deletions
  unintentionally mirrored to backup, retention-policy bugs). The
  inventory layer is the actual correctness verification when an
  install is at the scale where it's the right tool.
- **Promote process signals to a separate report.** Rejected: same
  daily cadence, same data inputs, same audience. Splitting adds
  cognitive load without separation of concerns.

## Consequences

- The report is useful for any install regardless of whether
  inventory is deployed: small installs that stay inventory-optional
  permanently, large installs that have deployed the pipeline,
  installs in transition.
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
- **Compatibility with [ADR-011](ADR-011-four-color-signal-taxonomy.md)**
  (proposed): the process classifier emits red / yellow / green from
  ADR-009's three-tier output. When the four-colour taxonomy
  (blue / green / amber / red) lands, both inventory and process
  classifiers migrate together. The thresholds defined here
  (`_BACKUP_AGE_RED_HOURS`, `_BACKUP_AGE_YELLOW_HOURS`) map cleanly
  onto the future AMBER / RED gradient.

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
