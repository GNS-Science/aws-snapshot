# ADR-006: Simplify storage tiers — drop Deep Archive, keep objects forever

- Status: Accepted (2026-05-25, implemented under #17)
- Date: 2026-05-19

> **Implementation note (2026-05-25):** Mitigations 1 (object-count delta in
> the daily health report) and 2 (manual-purge runbook
> `docs/operations/purge-from-backup.md`) were moved into the ADR-009
> implementation (#23) where the signal-classification redesign reshapes the
> delta check and gives the runbook its proper context. #17 ships the
> lifecycle change, the config cleanup, and the doc rewrite only.

## Context

The current backup bucket lifecycle policy has three storage tiers and an
expiry rule:

| Tier | Days | Storage class | NZD/GB/month |
|------|------|---------------|--------------|
| Hot | 0–30 | S3 Standard | $0.036 |
| Warm | 31–120 | Glacier Instant (`GLACIER_IR`) | $0.007 |
| Cold | 121–365 | Glacier Deep Archive (`DEEP_ARCHIVE`) | $0.0017 |
| Expire | 365+ | Deleted | — |

Three problems with this policy surfaced together during the 2026-05-19 health
check:

### 1. Annual silent re-copy of immutable data

The S3 incremental sync (`athena_inventory.py:248-256`) re-copies an object
only when its key is missing in the backup or size/ETag differs. Unchanged
objects are never re-touched, so their S3 `LastModified` timestamp never
resets. Once an object's `LastModified` passes `max_age_days` (365), the
Expiration rule deletes it. The next nightly diff sees `d.key IS NULL` and
re-copies it from the source, starting the cycle over.

For NSHM's largely-immutable scientific corpus this means **every stable
object is silently deleted and re-copied every year**, with a multi-hour
window each year where the backup of an immutable source object doesn't
exist. It also breaks the steady-state cost projection in
`docs/design/retention-strategy-and-costs.md`, which assumes "9 TB × 9
months in Deep Archive" — in reality each object cycles back to Standard
once per year.

### 2. Deep Archive restore is not implemented

`backup restore run` calls `s3:CopyObject` directly
(`src/aws_snapshot/s3_restore.py:191`). On a Deep Archive object that
returns `InvalidObjectState: The operation is not valid for the object's
storage class`. To actually restore a Deep Archive object the code would
need to:

1. Submit an S3 Batch Operations job of type `S3InitiateRestoreObject`
   with a `Tier` parameter (Standard = 12h thaw, Bulk = 48h thaw;
   Expedited is not available for Deep Archive).
2. Poll until thaw completes for every object in the manifest.
3. Then submit the existing CopyObject Batch job.

None of this exists. `commands/test.py:31,247,331` already knows the
problem — `test restore` *skips* archived objects with the note
"Glacier/Deep Archive — not directly copyable" — but `restore run` was
never wired up. As of today, any DR event involving objects older than
120 days would fail per-object until the thaw path is built.

### 3. Dead `cold_days` config key

`cold_days` is defined on both `LifecycleConfig` (`s3_backup.py:47`) and
the Pydantic config model (`config/models.py:20`), but
`apply_lifecycle_policy` never reads it. The Deep Archive transition day
is computed as `max(warm_days, hot_days + 90)`, so the config value
shown in `backup-config.production.yaml` has no effect on the deployed
policy. This was flagged by a team member reviewing the config.

## Decision

Simplify the lifecycle to two tiers, with no expiry:

| Tier | Days | Storage class | NZD/GB/month | Retrieval |
|------|------|---------------|--------------|-----------|
| Hot | 0–30 | S3 Standard | $0.036 | Immediate |
| Cold | 30+ (forever) | Glacier Instant (`GLACIER_IR`) | $0.007 | Milliseconds |

- **Drop the Deep Archive transition** entirely.
- **Drop the Expiration rule** for current object versions. Superseded
  versions remain governed by `NoncurrentVersionExpiration`
  (`version_retention_days`, default 365) — that mechanism is unchanged.
- **Remove `cold_days`** from `LifecycleConfig`, the Pydantic config model,
  and `backup-config.production.yaml`.
- **Keep `max_age_days`** in the config schema but document it as
  reserved for explicit retention windows on non-production sources; do
  not wire it to the production lifecycle.

## Cost impact

Steady-state storage cost for the 11.7 TB production corpus
(toshi 8 TB + ths 1 TB + static 2.7 TB + weka ~0):

| Component | Current (3-tier + expire) | Proposed (2-tier, no expire) | Δ |
|---|---|---|---|
| S3 storage | 11.7 TB × $0.0017 = ~$20/mo | 11.7 TB × $0.007 = ~$82/mo | +$62/mo |
| DynamoDB PITR + weekly export | $13/mo | $13/mo | — |
| S3 Batch + Lambda + EventBridge | $13/mo | $13/mo | — |
| **Total** | **~$47/mo (~$552/yr)** | **~$108/mo (~$1,300/yr)** | **+$748/yr** |

Context: the AWS Backup baseline was ~$1,700/mo. The proposed solution
is still ~16× cheaper than AWS Backup, versus ~36× cheaper today. The
absolute annual increase is ~NZD $750.

### DR retrieval impact (one-time, per restore event)

| Item | Current | Proposed |
|---|---|---|
| Retrieval fee (9 TB) | 9 TB × $0.126 = ~$1,130 | 9 TB × $0.079 = ~$709 |
| Thaw wait | **12–48 hours** | **milliseconds** |
| Restore code complexity | RestoreObject Batch job + polling (not built) | Direct CopyObject works as-is |

Each DR event saves ~$420 in retrieval fees and 12–48 hours of operator
wait time.

## What we get for the $748/year

1. **DR latency: 12–48 hours → milliseconds.** The Glacier IR class
   supports immediate reads. The `disaster-recovery-scenario.md` Phase 2
   estimate of "12–48 hours" collapses to the actual S3 copy throughput.
2. **No new restore code required.** `s3_restore.py` works against
   Glacier IR via `CopyObject` unchanged. The unimplemented
   `S3InitiateRestoreObject` Batch flow does not need to be built or
   tested.
3. **Simpler mental model.** Two tiers instead of three; the dead
   `cold_days` key is gone; the lifecycle policy is one transition
   instead of two plus an expiration.
4. **Eliminates the annual silent re-copy.** With no Expiration rule,
   immutable backup objects live forever in Glacier IR. The cost model
   becomes predictable and matches the steady-state projection.
5. **Removes the year-N cost cliff.** No future operator inherits a
   one-day storage spike when objects start cycling back to Standard.

## What we give up

- **~$748/year (NZD)** — real but small in absolute terms (~1 hour of
  engineering time per month).
- **The headline "36× cheaper than AWS Backup"** becomes
  "16× cheaper than AWS Backup." Still defensible.
- **No automatic cleanup floor.** If a production source is abandoned
  the backup bucket sits in Glacier IR indefinitely at $0.007/GB/month
  rather than aging out. The no-delete IAM policy already prevents
  automatic cleanup anyway, so this is a documentation change more than
  a behaviour change.
- **Intentional source deletions persist in backup forever.** If a team
  identifies and deletes a large chunk of source data as garbage (e.g.
  6 TB of bad-experiment outputs), the backup retains it indefinitely
  at ~$42/month for that 6 TB. Under the current 365-day Expiration
  policy the same garbage would age out within a year. This is a real
  trade — it is the symmetric cost of the "deleted source objects are
  protected from propagating to backups" guarantee.

  The backup engine is deliberately deaf to source deletions
  (`athena_inventory.py:248-256` only finds `source - backup`, never
  the inverse) and the Lambda role has no `s3:DeleteObject` on backup
  buckets. So there is no automated path to reflect intentional source
  deletions into the backup — and under no-Expiration, no lifecycle
  path either. Addressed by mitigations (1) and (2) below.

## Mitigations

Both mitigations were superseded / implemented under
[ADR-009](ADR-009-health-check-measurement-model.md) (#23):

1. **Visibility of source-side deletions.** ADR-009 introduces an
   asymmetric pair of source-vs-backup divergence signals:
   - `source - backup` (class 1, red) — backup is incomplete; the
     system has actually failed.
   - `backup - source` (class 2, informational) — orphan accumulation
     from source-side deletions; not an alarm.

   The original ADR-006 mit. 1 (a single `count_delta` threshold firing
   red on day-over-day source-count drops) is reclassified to class-2
   informational by ADR-009 — it was measuring the wrong thing for a
   delete-protected system. Operators see the same data, just no
   longer rendered as an alarm.

2. **Manual-purge runbook** — written as
   [`docs/operations/purge-from-backup.md`](../../operations/purge-from-backup.md)
   under ADR-009. Covers required IAM credentials, the small-list
   (`aws s3api delete-objects`) and large-list (S3 Batch) command
   patterns, the verification step, and the audit trail. Deliberately
   manual and friction-laden — not routine.

Together these give the system the property: orphans are visible
(mitigation 1, now class-2 info) and there is a documented way to act
on them (mitigation 2), but no daily code path is empowered to remove
backup data on its own.

## Alternatives considered

1. **Status quo (3 tiers + 365-day expiry).** Rejected — has the silent
   re-copy bug, the missing thaw code path, and the latent year-N cost
   cliff. The Deep Archive savings (~$60/mo) do not justify the
   engineering burden of correctly building and testing the thaw flow.
2. **Keep Deep Archive, build the thaw flow, drop only the Expiration
   rule.** Solves the silent re-copy and the cost cliff but still
   requires building, testing, and maintaining
   `S3InitiateRestoreObject` orchestration, polling, partial-thaw
   handling, and a documented 12–48h DR wait. Saves ~$60/mo at the cost
   of significant ongoing complexity.
3. **Increase `max_age_days` to 3,650 (10 years).** Defers the silent
   re-copy cliff to year 10 but doesn't eliminate it. A future operator
   inherits a sudden recopy spike without the context to recognise it.
   Rejected as deferring a problem rather than solving it.
4. **Keep a long Expiration rule (e.g. 7 years) explicitly as a garbage
   floor.** Would give intentionally-deleted source data an automatic
   age-out path so it doesn't accumulate forever. Rejected because (a)
   it reintroduces exactly the silent re-copy mechanism this ADR
   removes — at the 7-year mark, immutable scientific outputs would
   begin annual cycling — and (b) the same goal is served by the
   manual-purge runbook (mitigation 2) without the lifecycle complexity
   or the future cost cliff. The trade-off here was deliberate:
   prefer "deletions require human action" over "deletions happen
   automatically on a 7-year timer."
5. **Drop the Expiration rule only, keep Deep Archive.** Same as (2) —
   still requires the thaw code path.

## Implementation scope

| Component | File | Effort |
|-----------|------|--------|
| Remove Deep Archive transition + Expiration | `src/aws_snapshot/s3_backup.py` (`apply_lifecycle_policy`) | Trivial |
| Remove `cold_days` from `LifecycleConfig` | `src/aws_snapshot/s3_backup.py` | Trivial |
| Remove `cold_days` from Pydantic model | `src/aws_snapshot/config/models.py` | Trivial |
| Remove `cold_days` from production config | `backup-config.production.yaml` | Trivial |
| Update tests for new lifecycle shape | `tests/test_s3_backup.py`, `tests/test_config.py`, `tests/test_cli.py` | Small |
| Update storage-tier table | `docs/architecture/storage-tiers.md` | Small |
| Update lifecycle-tier table | `docs/how_it_works.md` | Small |
| Update cost model | `docs/architecture/cost-model.md` | Small |
| Update retention-strategy doc | `docs/design/retention-strategy-and-costs.md` | Small |
| Update DR scenario doc (Phase 2 collapses) | `docs/design/disaster-recovery-scenario.md` | Small |
| Re-apply lifecycle policy to deployed buckets | One-off via `backup setup` or `aws s3api put-bucket-lifecycle-configuration` | Small |
| Mitigation 1: object-count delta in health report | `src/aws_snapshot/health_report.py` (see ADR-005) | Small |
| Mitigation 2: manual-purge runbook | `docs/operations/purge-from-backup.md` (new) | Small |

### Migration of existing data

Objects already in Deep Archive at the time of the policy change will
**not** automatically move back to Glacier IR — lifecycle transitions
are one-way. They can either:

- Be left in Deep Archive (free, but they retain the slower retrieval
  characteristic forever). At restore time the new code would still
  need to handle them as a tail case.
- Be explicitly transitioned back via an S3 Batch Operations
  `Restore + Copy` job (cost: ~$1,130 retrieval fee for 9 TB, but
  brings the entire corpus into IR uniformly).

**Recommendation:** apply the new policy now so newly-written and
recently-warm objects stay in IR going forward. Defer the bulk
re-transition decision to a separate operational task — production
sources have only had Deep Archive transitions for ~1 month, so the
volume of currently-archived data is small.

## Risks

- **Annual cost increase is real.** ~NZD $750/year is small but
  perpetual; needs to be reflected in the project's running-cost
  documentation and any GNS internal budgeting.
- **One-way lifecycle migration.** Objects currently in Deep Archive
  stay there unless explicitly transitioned. Tracked under "Migration
  of existing data" above.
- **Future scientific datasets may be much larger.** A 100 TB future
  corpus at Glacier IR would cost ~$700/mo vs ~$170/mo at Deep Archive
  — the trade may need to be revisited if storage volume grows by an
  order of magnitude.

## Possible future enhancements

- **Per-source `hot_days` override.** `RetentionConfig` currently applies
  globally. If a source ever wants a different transition threshold (e.g.
  `static` at 7 days vs `toshi` at 30), add an optional `retention:` block
  on `SourceConfig` and have `backup setup lifecycle` fall back to the
  root value when not set. Not pursued now — the single global knob is
  sufficient for current usage, and the per-source naming convention
  (`get_backup_bucket_name`) already keeps the policy boundary clean per
  bucket if we ever do split it.

## Links

- Storage tiers: `docs/architecture/storage-tiers.md`
- Cost model: `docs/architecture/cost-model.md`
- Retention strategy: `docs/design/retention-strategy-and-costs.md`
- DR scenario: `docs/design/disaster-recovery-scenario.md`
- Lifecycle code: `src/aws_snapshot/s3_backup.py:171-215`
- Diff predicate: `src/aws_snapshot/athena_inventory.py:248-256`
