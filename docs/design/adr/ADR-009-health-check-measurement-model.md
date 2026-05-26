# ADR-009: Health-check measurement model

- Status: Accepted (2026-05-25, implemented under #23)
- Date: 2026-05-25

> **Implementation note (2026-05-25):** Class-1 backup-missing signal,
> class-2 reclassification of the source-count delta, the orphan-count
> signal, and the manual-purge runbook (`docs/operations/purge-from-backup.md`)
> all ship together. Source-vs-backup divergence is computed by a single
> Athena query (`_build_divergence_count_query`) that returns both
> directions in one scan. `delta_pct_threshold` / `delta_abs_threshold`
> were removed from `HealthReportConfig` and the production YAML — they
> no longer apply once the signal is informational.

## Context

ADR-005 specified *how* the daily report is delivered (Slack + SNS). ADR-006
mitigation 1 and ADR-007 mitigation 4 each introduced a specific signal —
object-count delta and inventory freshness respectively. ADR-005's classifier
then mapped each signal to green/yellow/red. None of the prior ADRs defined
the underlying *model*: what each signal actually means to the operator, and
what concept of "the backup system is healthy" the report is testing.

PR #21 review (@chrisdicaprio, 2026-05-24) exposed the design weakness with a
concrete scenario:

> "If objects are write once, why should we allow any decrease in size or
> object count?"

Walking through what would happen if NSHM cleaned up 10,000 stale science
reports from a source bucket:

| Day | Source | Backup | Current report | What it means |
|---|---|---|---|---|
| 0 | 1,000,000 | 1,000,000 | 🟢 | steady-state |
| 1 (cleanup) | 990,000 | 1,000,000 | 🔴 ("source dropped 10k") | source changed |
| 2+ | 990,000 | 1,000,000 (10k orphans) | 🟢 | source-side daily delta is zero again |

The current `count_delta` check measures **source-side change**, not **backup-
system correctness**. Cleanup events fire a one-day red spike that the
operator dismisses. Meanwhile the orphan accumulation in backup — the actual
operationally interesting state — is invisible to the report. And the signal
that would mean "the backup system has actually failed" (backup missing keys
that source has) isn't measured at all.

For a delete-protected backup system where backup never decreases, source-
side change is normal operational news. The thing worth measuring is whether
backup is keeping up with source (class 1 below), and separately whether the
gap in the other direction is growing (class 2).

## Decision

Define three signal classes. Map each existing and proposed metric to one.
Render the report layout to make the distinction visible to operators.

### Signal classes

**Class 1 — Backup-system correctness** (failure of the system's primary job)
- Restore-test failure (sampled objects unrecoverable)
- DynamoDB PITR disabled on a configured table
- Inventory missing entirely (`backup status` cannot determine state)
- **NEW**: source has keys backup doesn't — i.e. backup is incomplete
  (Athena: `count(source.keys) - count(backup.keys ∩ source.keys) > 0`,
  excluding operational prefixes per the existing filter)

Class 1 firing always means red on the affected source.

**Class 2 — Operational news** (source-side state changes; informational)
- Source count changed vs yesterday (drop or growth)
- **NEW**: backup has keys source doesn't — i.e. orphan accumulation from
  source-side deletions (Athena: `count(backup.keys) - count(source.keys ∩
  backup.keys) > 0`)

Class 2 never changes the report's headline status. Always visible in the
per-source row but rendered distinctly (e.g. cyan/grey) and never red.

**Class 3 — Forward-looking risk** (system functioning but trend is concerning)
- Inventory freshness > 30h (system is operating on stale data)
- (Future) Athena scan-bytes trending up, S3 Batch fee accumulation, etc.

Class 3 firing means yellow on the affected source (unless class 1 also fires,
in which case red wins).

### Per-source classifier

```
red    if any class-1 signal fires
yellow if any class-3 signal fires AND no class-1
green  otherwise

class-2 signals never affect colour; always rendered in the row.
```

### Report layout

Each per-source row gains a "notes" section for class-2 lines. Example:

```
🟢 toshi    inventory_age=3h   restore=passed
              ℹ source grew by 47 objects today
              ℹ backup has 12,431 orphans (source cleanups since 2026-04-12)

🔴 ths      inventory_age=3h   restore=FAILED
              ℹ source grew by 0 objects today
              ⚠ backup is missing 3 source keys (last copy: 2026-05-23)
```

Subject line / headline reflects only class 1 + class 3 (green/yellow/red);
operator scans class-2 lines in the body if curious.

### What changes for the existing source-delta check

ADR-006 mitigation 1's "source-count drop" check was attempting to be class 1
but is actually class 2 by this taxonomy. Reclassify:

- Existing day-over-day source delta: stays in the report as class-2
  informational (cleanups + organic source growth both visible). Stops
  firing red. Issue #23's threshold-tightening becomes irrelevant.
- New class-1 signal (`source - backup`) is introduced: backup-missing-source-
  keys is what *would* indicate the backup system has failed.

### Orphan management

The new class-2 "backup has N orphans" signal makes orphan accumulation
visible for the first time. Decision about whether/when to remove orphans
remains operator judgement, not health-report logic. Two paths:

- **Do nothing** — orphans age out via the 365-day Expiration rule (current
  production policy). The class-2 line stays visible but isn't a problem.
- **Manual purge** — operator runs a documented procedure to delete specific
  orphans intentionally.

A runbook for the manual-purge path was referenced as ADR-006 mit. 2 but
never written. This ADR adopts it as in-scope work:
`docs/operations/purge-from-backup.md` to be a deliverable of this ADR's
implementation. Exact runbook content is deferred to the implementation
phase — its shape depends on factors better answered with a real signal in
front of us:

- Whether typical orphan lists are small (one-off `aws s3 rm` is fine) or
  large enough to need S3 Batch Operations.
- How operators want to confirm intent: interactive prompt, signed
  manifest, two-person review.

The runbook will live with the production-config workflow rather than as
an abstract spec.

## Alternatives considered

1. **Keep all signals red-equivalent (current state).** Operator can't
   distinguish "source was cleaned up" from "backup has failed"; alert-
   fatigue risk as cleanups happen more than failures. Rejected.

2. **Drop the source-delta check entirely.** Loses useful operational
   visibility (cleanups are worth knowing about and a sudden growth spike
   may correlate with source-side load). Rejected — reclassifying to class-
   2 informational is the right home.

3. **Per-signal severity in config.** Let operators tag each signal as
   red/yellow/informational in YAML. Over-engineered for the current
   handful of signals; revisit if signals proliferate enough to warrant
   it.

4. **Issue #23 as originally scoped** (just tighten the source-delta
   thresholds). This was solving the wrong problem — making class-2 alarms
   more noisy rather than introducing the missing class-1 signal.
   Superseded by this ADR; #23 to be closed as obsolete.

5. **One big "source-vs-backup divergence" signal that combines both
   directions.** Simpler to render but loses the asymmetry that matters:
   one direction is a real failure, the other is just news. Rejected.

## Implementation scope

| Component | File | Effort |
|---|---|---|
| Two new Athena queries (`count(source - backup)`, `count(backup - source)`) | `src/nzshm_backup/athena_inventory.py` | Medium |
| `SourceHealthData` gains `backup_missing_count`, `backup_orphan_count`, and class fields | `src/nzshm_backup/health_report.py` | Small |
| Per-source classifier rewritten per the rules above | `src/nzshm_backup/health_report.py` | Small |
| Slack + email formatters render class-2 distinctly | `src/nzshm_backup/health_report.py` | Small |
| Existing source-delta check repurposed (class-2 informational; stops firing red) | `src/nzshm_backup/health_report.py` + `backup-config.production.yaml` | Trivial |
| Manual-purge runbook | `docs/operations/purge-from-backup.md` (new) | Medium |
| ADR-006 mit. 1, ADR-007 mit. 4 cross-reference this ADR | those ADR files | Trivial |
| #23 closed as superseded | GitHub | Trivial |
| User-guide health-report doc updated for class taxonomy | `docs/user-guide/health-report.md` | Small |
| Unit tests for new signals + classifier | `tests/test_health_report.py` | Small |
| Per-source `inventory_enabled` opt-out (no-Inventory floor mode) | `src/nzshm_backup/config/models.py` + `health_report.py` + tests | Small |

## Consequences

- **Operator sees a sharper picture.** "What failed" is the headline; "what
  changed" is in the body. Reduces alert fatigue from operationally normal
  events being conflated with real failures.
- **The class-1 signal that actually matters is finally measured.** Backup-
  missing-source-keys was never directly checked; the only thing catching it
  was the implicit "next nightly sync will copy missing keys" — which
  doesn't help if Athena diff itself is broken.
- **The manual-purge runbook becomes real.** Previously a referenced-but-
  unwritten ADR-006 mitigation; this ADR adopts it as deliverable work.
- **Two extra Athena queries per source per day** — ~$0.01 incremental cost
  per report run if scan bytes stay bounded (same workgroup cap as the
  existing manifest pipeline).
- **Report layout is busier** — one or two additional rows per source for
  class-2 lines. Mitigated by rendering them distinctly so they're scannable
  rather than alarming.

## Risks

- **Class-2 signals get ignored entirely.** Mitigation: render them inline
  within each per-source row (not in a separate footer block); operators
  scan them while reading the source's status naturally. Use distinct icon
  (ℹ) and never red colour.
- **Source-vs-backup Athena queries scan a lot on large buckets** (toshi
  8M, static 40M). Mitigation: pin to the same Athena workgroup as the
  manifest pipeline (existing scan-bytes cap applies). Add a per-source
  Athena cost monitor in a follow-up if scan bytes drift up.
- **No production data for class-1 backup-missing signal today.** The
  scenario should be impossible under the current Lambda implementation
  (it only adds objects). Mitigation: synthetic test in sandbox during
  implementation by deleting then re-adding a single key.
- **The manual-purge runbook shape is unknown.** Sized as "Medium" but may
  expand if operators want approval workflows. Mitigation: ship a minimal
  CLI-only runbook first; iterate based on first real use.

## Links

- PR #21 review thread that exposed the issue —
  https://github.com/GNS-Science/nzshm-backup/pull/21#discussion_r3295475049
- Issue #23 — to be closed as superseded by this ADR
- ADR-005, ADR-006 mit. 1, ADR-007 mit. 4 — existing signals being
  recategorised by this ADR
- `docs/user-guide/health-report.md` — operator-facing doc to be updated
  when this ADR is implemented
