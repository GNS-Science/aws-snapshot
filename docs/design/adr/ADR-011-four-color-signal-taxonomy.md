# ADR-011: Four-colour signal taxonomy (blue / green / amber / red)

- Status: Proposed
- Date: 2026-06-10

> **2026-06-27 forward-compatibility note — see
> [ADR-014](ADR-014-inventory-optional-health-signals.md).** ADR-014
> introduces a process-signal classifier producing red / yellow /
> green; the implementation deliberately uses ADR-009's three-tier
> output so that when this ADR (four-colour) lands, both inventory
> and process signal classifiers migrate together. The thresholds in
> ADR-014 (`_BACKUP_AGE_RED_HOURS=36`, `_BACKUP_AGE_YELLOW_HOURS=12`)
> would map cleanly onto the AMBER / RED gradient once the four-tier
> classifier is in place.

## Context

[ADR-009](ADR-009-health-check-measurement-model.md) established a three-tier
classifier (`red` / `yellow` / `green`) mapped onto three signal classes:

- **Class 1** → RED (the backup system has failed)
- **Class 2** → no colour, rendered as `ℹ` info notes (operational news)
- **Class 3** → YELLOW (forward-looking risk; functioning-but-stale)

This worked cleanly under the original assumption that class-1 RED
fires only when something is actually broken. The 2026-06-03 Cycle-1
validation surfaced the **snapshot-vs-backup race** finding (recorded
in PROD-DEPLOY-LOG Step 21):

> When source receives new writes between the daily inventory scan and
> the next scheduled backup run, the report fires class-1 ⚠ RED with
> `(auto-healed since snapshot, sampled N)` the following morning. The
> head-check tag correctly identifies the gap as already-repaired, but
> the row is still RED.

The 2026-06-09 Cycle-2 validation re-exhibited this with a different
trigger (new source writes rather than admin-delete), confirming the
pattern is **structural**: any source on a continuous-write cadence
will produce a daily class-1 RED with the auto-healed tag. The current
production sources don't exhibit this only because their writes are
bursty (analysis-run-driven). If any source ever switches to a
continuous-write pattern (a nightly automated results pipeline, say),
the daily report would show RED *every day* — diluting the value of
RED as a "fix this now" signal.

We also acknowledged in ADR-009's *Risks* section that class-2 ℹ
informational signals might be ignored entirely. In practice operators
have to scan past the row colour to read the ℹ sub-lines — easy to
miss orphan-count growth or unusual source-delta when scrolling
through a green-headlined report.

A four-colour taxonomy addresses both problems:

- **Auto-healed since snapshot** demotes to AMBER (an anomaly worth
  noting, not a failure)
- **Class-2 informational** signals promote to AMBER (visible in the
  row colour, no longer hidden in sub-lines only)
- **No-activity steady state** distinguishes from active-and-healthy
  via BLUE — useful for explicitly idle sources

## Decision

Replace the three-tier classifier with a four-tier severity gradient:

| Colour | Severity | Meaning | Operator action |
|---|---|---|---|
| 🔵 **BLUE** | None | System is alive, ready, no activity to process | None — informational; quiet is fine |
| 🟢 **GREEN** | None | System is healthy and processed work successfully today | None |
| 🟠 **AMBER** | Low | Anomaly detected; may already be resolved or may need investigation | Read the row's notes; decide whether to investigate further |
| 🔴 **RED** | High | Genuine failure; the backup system's primary job has demonstrably failed | Investigate and fix now |

### Per-source classifier (replaces ADR-009 §"Per-source classifier")

```
red    if any of:
         - restore-test failed
         - PITR disabled on any configured DynamoDB table
         - inventory missing entirely (no manifest ever delivered)
         - backup is missing keys that source has, sampled keys still
           404 live (head-check confirms current gap)

amber  if no red AND any of:
         - backup is missing keys that source has, sampled keys all
           200 live ("auto-healed since snapshot")
         - inventory freshness > threshold (forward-looking risk)
         - backup-orphan count > 0 (source-side deletions retained)
         - source-count delta != 0 vs yesterday (any non-zero change)

blue   if no red AND no amber AND all of:
         - source count delta == 0
         - backup orphan count unchanged from yesterday
         - inventory fresh (≤ threshold)
         - restore-test passed (when scheduled to run)
         - PITR enabled (when DynamoDB tables present)

green  otherwise (active, healthy work — at least one signal of
       activity but nothing concerning)
```

The classifier short-circuits in the order above — RED beats AMBER
beats GREEN beats BLUE on a per-source row.

### Headline / overall classification

Headline precedence: **`red > amber > green > blue`**. The headline
reflects the *worst* state across all sources. Numerator format:

```
NSHM backup health 2026-06-10 — AMBER  (3 🟢 / 2 🟠 / 1 🔵 / 0 🔴 of 6)
```

Email subject line:

```
Subject: NSHM backup health 2026-06-10 — AMBER (3 GREEN / 2 AMBER / 1 BLUE)
```

When every source is BLUE, the headline is `BLUE` — a meaningful
distinction from `GREEN` because it explicitly communicates "the
system is alive and ready, but had nothing to do."

### Mapping today's signals to the new taxonomy

| Signal | Class (ADR-009) | Pre-ADR-011 colour | Post-ADR-011 colour |
|---|---|---|---|
| Restore-test failure | 1 | RED | RED |
| PITR disabled | 1 | RED | RED |
| `no inventory data available` (true missing) | 1 | RED | RED |
| `backup is missing N keys (still missing live)` | 1 | RED | RED |
| `backup is missing N keys (auto-healed since snapshot)` | 1 | RED | **AMBER** ← change |
| Inventory > 30h stale | 3 | YELLOW | AMBER (rename + same severity) |
| `backup has N orphans` | 2 | no colour (ℹ) | **AMBER** ← change |
| `source grew/dropped by N objects` | 2 | no colour (ℹ) | **AMBER** ← change |
| Source count_delta == 0, all signals clean | — | GREEN | **BLUE** ← new state |
| Active healthy with new writes copied | — | GREEN | GREEN |
| `inventory disabled for this source` (toy-noinv pattern) | 2 | no colour (ℹ) | BLUE — by design, the source is intentionally floor-mode and quiet |

### Row sub-line glyphs

Sub-line glyphs continue to render under the row, but are now
*explanatory* rather than the primary signal of class-2 information.
The row colour carries the severity:

- `⚠` on RED or AMBER rows for failure/anomaly notes
- `ℹ` only for purely descriptive context that the operator may want
  but never needs to act on (e.g. `ℹ Today's rotated source: toshi`)

The vast majority of current `ℹ` lines are about orphans or source
delta — those rise to AMBER row colour, and their detail line drops
the ℹ glyph in favour of `⚠`.

## Cost impact

No incremental Athena or S3 cost. The signal-collection code paths
are unchanged — only the classifier and formatter logic change.
The new BLUE classification needs one additional comparison
(yesterday's orphan count) but that's already cached in count_delta's
results — no extra query.

## Consequences

### Positive

1. **`(auto-healed since snapshot)` is no longer a daily RED.** Operators
   can scan a row's colour and know whether to drop everything (RED)
   or note-and-continue (AMBER). The audit framing is preserved via
   the existing tag; we're changing the *triage urgency* not the
   *audit fact*.
2. **Class-2 ℹ signals become visible.** Orphan-count growth and
   source-delta no longer hide in sub-lines that operators might
   skip. Promoting them to AMBER row colour ensures they get scanned.
3. **BLUE distinguishes idle from active.** Three useful operator
   interpretations:
   - "All sources BLUE today" → quiet day, no work, system verified
     working but idle. Different conversation from "all GREEN with
     active backup activity."
   - "One source switched from BLUE to GREEN" → its upstream
     started writing data. Worth knowing about.
   - "One source switched from GREEN to BLUE" → its upstream
     stopped writing. Possibly a problem worth flagging if the source
     is expected to be active. (Detection-of-no-activity isn't strictly
     this system's job, but the visibility helps.)
4. **Severity gradient maps to operator behaviour.** RED → page. AMBER
   → review during the workday. GREEN → glance, move on. BLUE →
   archive without reading. Each colour answers a different
   action-urgency question cleanly.
5. **Slack-native colours.** Slack supports the four primary status
   emoji (`:large_red_circle:`, `:large_orange_circle:`,
   `:large_green_circle:`, `:large_blue_circle:`) directly. No
   third-party emoji needed.

### Negative

1. **Re-training cost.** Existing operators have a mental model from
   ADR-009's three-tier system; the new fourth state needs a brief
   explanation. Mitigated by a transition-window subject-line prefix
   (e.g. "[NEW] AMBER includes orphans and auto-healed").
2. **More colour to interpret per report.** Six sources × four possible
   colours = more visual variety. Mostly harmless given the worst-case
   triage is still "find the RED" — same as before.
3. **Headline RED loses some sources.** Today's example: toy-inv would
   be AMBER under the new rules (`auto-healed since snapshot`), so
   today's report headline would be AMBER 5🟢/1🟠 rather than RED
   5/6. Some teams might prefer the existing "any anomaly = RED" rule
   for simplicity. Counter-argument: that rule produces alert fatigue
   on routine self-healing events — exactly the problem this ADR is
   solving.
4. **Audit framing slightly softer.** ADR-009 explicitly kept RED for
   "gap existed at snapshot time" regardless of live state, preserving
   audit weight. This ADR demotes that to AMBER. Mitigation: the *tag*
   `(auto-healed since snapshot)` remains in the note line — auditors
   reading the report still see the gap-existed evidence. The colour
   change is for operator triage, not record-keeping.

### Risks

- **Drift in what "BLUE" means.** Without strict rules, BLUE could
  expand to mean "GREEN that's quieter" — losing the clean
  no-activity definition. Mitigation: the classifier rules above are
  the canonical source-of-truth; the user-guide must state them
  explicitly and call out the difference from GREEN.
- **A new colour means new tests.** ~10 existing classifier tests need
  to re-assert against the four colours; new tests needed for the
  AMBER auto-healed path, BLUE idle path, GREEN active-but-no-anomaly
  path. Bounded but real.
- **Headline math affects subscribers.** Some recipients may have email
  filters or Slack-channel rules keyed on the literal string `RED` or
  `GREEN`. AMBER is a new word in the alert vocabulary. Mitigation:
  one-line heads-up message before deploy, plus the email subject
  retains backwards-compatible information ("(3 GREEN / 2 AMBER / 1
  BLUE)").

## Alternatives considered

1. **Keep ADR-009's three-tier system, accept the daily auto-healed RED
   as the cost of audit framing.** Working today; honest about its
   noise. Rejected because that noise will get worse as production
   sources move toward continuous-write patterns (already happening
   in adjacent infrastructure projects).

2. **Three-tier with AMBER replacing YELLOW, no BLUE.** Just rename
   YELLOW → AMBER and demote auto-healed + class-2 to AMBER. Captures
   most of the value with a smaller change. Rejected because BLUE
   adds genuine operator-clarity for the "system idle" state, and
   adding it now is cheaper than adding it later.

3. **Three-tier with a separate "info badge" in addition to row
   colour.** E.g. GREEN + 🟠 badge for "has informational alerts."
   Effectively a parallel orthogonal axis. Rejected as more visual
   complexity than just promoting to a fourth row colour.

4. **Per-source colour preferences in config.** Let each source
   override the colour rules. Rejected — undermines the global
   semantic of each colour and creates per-source operator-training
   debt.

5. **Five-tier: BLUE / GREEN / AMBER / RED / CRITICAL.** Distinguish
   "fix today" from "wake someone up." Rejected for now — the
   fast-path Lambda-error alarm (ADR-005) already covers the
   "wake-on-fire" niche. Could revisit if we ever consolidate fast
   and slow paths.

6. **Use class-2 signals to drive AMBER but keep auto-healed at RED.**
   Treats audit framing as inviolable. Rejected because that's
   exactly the alert-fatigue trap the ADR is trying to escape; the
   tag preserves audit fact, the colour conveys triage urgency.

## Implementation scope

| Component | File | Effort |
|---|---|---|
| Add `BLUE` literal to `Status` type | `src/aws_snapshot/health_report.py` | Trivial |
| Rewrite `_classify_source` for 4-colour rules | `src/aws_snapshot/health_report.py` | Small |
| `SourceHealthData` may gain `prev_orphan_count` for delta detection | `src/aws_snapshot/health_report.py` | Small |
| Headline math: rewrite `HealthReportData.overall` for 4-colour precedence | `src/aws_snapshot/health_report.py` | Trivial |
| Slack formatter: 4 emoji + per-row colour bar (existing pattern) | `src/aws_snapshot/health_report.py` | Small |
| Email formatter: 4-character glyph + headline math | `src/aws_snapshot/health_report.py` | Small |
| Subject-line format: `(M GREEN / N AMBER / K BLUE / J RED of TOTAL)` | `src/aws_snapshot/health_report.py` | Small |
| Update existing classifier tests (~10 tests) | `tests/test_health_report.py` | Small |
| New tests: AMBER auto-healed path, BLUE idle path, GREEN active path | `tests/test_health_report.py` | Small |
| Update `ADR-009` with a "Superseded in part by ADR-011" header | `docs/design/adr/ADR-009-…md` | Trivial |
| User-guide health-report doc — 4-colour section + revised investigate-by-signal tables | `docs/user-guide/health-report.md` | Medium |
| Cheatsheet entry for triage-by-colour | `docs/operations/cheatsheet.md` | Small |
| Sandbox runbook — re-predict scenarios under 4-colour rules | `docs/operations/health-signal-validation-sandbox.md` | Medium |
| ADR-011 doc itself | this file | (done) |
| ADR README index update | `docs/design/adr/README.md` | Trivial |
| Subscriber heads-up message before deploy | (operational; one Slack post) | Trivial |
| **Total estimate** | | **~80% the size of ADR-009 implementation** |

### Roll-out sequence

1. Land this ADR (Proposed → Accepted after team review)
2. Implementation in a single PR — code + tests + docs together
3. Deploy to Lambda alongside subscriber heads-up
4. Watch one week of production reports to confirm the new headline
   math behaves and operators are reading the new states correctly
5. Status → "Adopted in production" with a PROD-DEPLOY-LOG entry

## Open questions

1. **Delta-detection for BLUE** — does "no source activity today"
   require previous-day comparison (yesterday's count_delta), or can
   we infer from today's snapshot alone? The simplest answer is
   "today's count_delta is zero" but that doesn't quite catch "source
   had +5 then -5 in 24h." Probably acceptable; refine if observed
   to matter.

2. **Auto-healed sampling fidelity** — currently we sample 10 keys
   for the head-check. What about cases where 10 of 100 missing keys
   sampled all return 200, but the other 90 are still missing? Sample
   says "all auto-healed" → AMBER, but real state is RED-worthy. Not
   a new problem (today's sample-of-10 has the same property), just
   worth flagging — the auto-healed tag inherits sampling
   uncertainty. Mitigation: tag includes `(sampled N)` so operators
   know the basis.

3. **`inventory_disabled` floor mode colour** — toy-noinv pattern.
   Currently GREEN with `ℹ inventory disabled` note. Two options:
   - BLUE — "system is idle by design, restore-test is the dominant
     signal"
   - GREEN with informational note (status quo)
   This ADR proposes BLUE on the grounds that an explicit floor-mode
   source is functionally idle from an inventory-pipeline
   perspective. Open for discussion.

## Links

- [ADR-009: Health-check signal-class taxonomy](ADR-009-health-check-measurement-model.md) — this ADR builds on and partially supersedes ADR-009's classifier
- [PROD-DEPLOY-LOG Step 21](../../PROD-DEPLOY-LOG.md) — snapshot-vs-backup-race finding that motivates the auto-healed-as-AMBER change
- [Health Report user guide](../../user-guide/health-report.md) — will need a substantive rewrite if this ADR is accepted
- [Sandbox runbook](../../operations/health-signal-validation-sandbox.md) — scenarios will need re-prediction under the new rules
