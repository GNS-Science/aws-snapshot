# ADR-010: Apply Intelligent-Tiering to source buckets (toshi-api + ths only)

- Status: Proposed
- Date: 2026-06-08

## Context

[ADR-006](ADR-006-simplify-storage-tiers-drop-deep-archive.md) moved the
backup buckets to a single Standard → Glacier Instant Retrieval (GIR)
transition at day 30. The post-migration cost picture (validated via
AWS Cost Explorer on 2026-06-06: $300 one-off transition spike,
$12 → <$6/day baseline drop = ~$216/mo recurring saving) confirmed the
math.

Looking at AWS Cost Explorer alongside the backup-side win, the
**source buckets are now disproportionately expensive**. They sit on
S3 Standard at $0.025/GB/mo for ~11.7 TB of write-once-read-rarely
scientific data — costing ~$300/mo just for storage. The same data
in the backup bucket now costs ~$84/mo (after the GIR migration).

This ADR asks: can we get similar savings on the source side without
disrupting the daily backup pipeline (Inventory + Athena divergence +
incremental CopyObject sync) or the live API/analysis read paths?

### Why not just replicate the backup-side approach (GIR @ 30d)?

GIR has a **per-GB retrieval fee** ($0.012/GB) that fires on every
GET. Source buckets serve production read traffic — analyses,
re-runs, occasional re-analysis sweeps — and a single sweep over even
a fraction of the corpus could burn through the storage savings.

A safer alternative is **S3 Intelligent-Tiering**:

- Automatic tier transitions based on actual access patterns
- No retrieval fees (built into the tiered pricing)
- Auto-promotes accessed objects back to Frequent Access (no charge)
- Latency: instant retrieval on all sub-tiers, same as Standard
- Backward-compatible with our backup pipeline (Inventory + ETag
  comparison + CopyObject all tier-agnostic)

The trade-offs are:

- $0.0025 per 1,000 objects/month monitoring fee (scales with object
  count, not bytes)
- 128 KB minimum billable size on the IA + Archive Instant sub-tiers
  (objects smaller are billed at 128 KB)
- 90-day minimum storage duration once an object moves into a
  cold sub-tier (prevents instant-revert pump-and-dump)

These trade-offs decide which source buckets are good fits and which
aren't.

## Decision

Apply Intelligent-Tiering via a `transition: 0 days → INTELLIGENT_TIERING`
lifecycle rule to **toshi-api-prod** and **ths-dataset-prod** only.
Leave **static-reports** on Standard. Leave **weka-ui-prod** alone
(trivial size).

## Cost model

### Access-pattern assumption

Working with the team's read characterisation: roughly **1% of corpus
is "warm" (touched within last 30 days), 99% is cold and stays cold
across 90-day windows**. After 90 days idle, Intelligent-Tiering
auto-moves to Archive Instant; in steady state the corpus distribution
is approximately:

- 1% Frequent Access (recently-written + re-accessed)
- 99% Archive Instant Access ($0.004/GB/mo)

The transient Infrequent Access tier (30–90 days idle) holds a small
sliver — ignored in the model because the volume is tiny and the
rate is intermediate.

### Per-source projections (ap-southeast-2 pricing, annualised)

#### toshi-api-prod — 8 TB / ~7M objects (avg 1.17 MB)

| Component | Standard | Intelligent-Tiering (1/99) |
|---|---|---|
| Storage subtotal | $204.80/mo | $34.49/mo |
| Monitoring (7M × $0.0025/1000) | — | $17.50/mo |
| **Monthly** | **$204.80** | **$51.99** |
| **Annual** | **$2,458** | **$624** |
| **Annual saving** | | **~$1,834** ✓ |

#### ths-dataset-prod — 1 TB / ~4M objects (avg 256 KB)

| Component | Standard | Intelligent-Tiering (1/99) |
|---|---|---|
| Storage subtotal | $25.60/mo | $4.31/mo |
| Monitoring (4M × $0.0025/1000) | — | $10.00/mo |
| **Monthly** | **$25.60** | **$14.31** |
| **Annual** | **$307** | **$172** |
| **Annual saving** | | **~$135** ✓ |

#### static-reports — 2.7 TB / 40M objects (avg ~70 KB) — EXCLUDED

| Component | Standard | Intelligent-Tiering (1/99) |
|---|---|---|
| Storage subtotal | $69.13/mo | $20.96/mo |
| Monitoring (40M × $0.0025/1000) | — | **$100.00/mo** |
| **Monthly** | **$69.13** | **$120.96** |
| **Annual delta** | | **−$622** ✗ |

The 40M-object monitoring fee plus the 128 KB minimum size tax
(actual avg 70 KB → billed at 128 KB = 1.83× inflation on cold tiers)
compound to make Intelligent-Tiering a net loss for static-reports
**regardless of access pattern**. Stays on Standard.

#### weka-ui-prod — trivial

Few small objects. Savings or losses both <$5/year. Skip.

### Total expected impact

| Scope | Monthly net | Annual net |
|---|---|---|
| toshi + ths | +$164.10/mo | **+$1,969/year** ✓ |
| (Hypothetical: all sources) | +$112.27/mo | +$1,347/year (static loss eats $622) |

### One-off transition cost

Lifecycle transitions to Intelligent-Tiering cost $0.01 per 1,000
requests:

- toshi: 7M × $0.01/1000 = ~$70
- ths: 4M × $0.01/1000 = ~$40
- **Total one-off: ~$110**

Payback: ~3 weeks from ongoing savings.

### Sensitivity analysis (toshi only)

The 1/99 assumption is the dominant variable. Sensitivity:

| Scenario | Frequent/Archive split | Monthly saving | Annual saving |
|---|---|---|---|
| Conservative (warmer) | 30/70 | $87/mo | $1,044/year |
| Realistic | 20/80 | $99/mo | $1,191/year |
| **Team's estimate** | **1/99** | **$153/mo** | **$1,834/year** |
| Pure cold | 0/100 | $156/mo | $1,876/year |

Even the conservative case yields ~$1,000/year on toshi alone, so the
ADR's value proposition holds across reasonable uncertainty in the
access pattern.

## Consequences

### Positive

1. **~$1,970/year ongoing saving** for ~$110 one-off + zero ongoing
   operational cost (lifecycle rules are AWS-managed).
2. **No retrieval-fee risk** — unlike a blanket GIR on source, no read
   pattern can cause cost spikes. Re-analysis sweeps are free.
3. **Self-tuning** — if access patterns change (e.g. a new use-case
   re-reads old toshi BLOBs), AWS auto-promotes them back to Frequent
   on access. No human intervention to re-tier.
4. **Backup pipeline unaffected** — Inventory + Athena divergence + ETag-
   based incremental CopyObject all work identically on
   Intelligent-Tiering. See [Risks](#risks) section for the one minor
   auto-promotion footnote.
5. **Reversible** — a `lifecycle remove` puts new writes back on
   Standard; existing tiered objects move back to Frequent on next
   access (and stay there 30+ days).

### Negative

1. **Slight added complexity** in cost reporting — Cost Explorer
   line items grow from one (Standard) to up to four
   (Frequent + IA + Archive Instant + Monitoring) per bucket. Not
   a blocker.
2. **90-day minimum storage** in any cold sub-tier — irrelevant for
   write-once data, but worth knowing for any future workflow that
   would delete or overwrite within 90 days.

### Source-bucket access-pattern observation

This ADR's adoption commits the team to the 1/99 read characterisation
as a working assumption. If a major new use-case (e.g. a continuous
re-analysis pipeline that re-reads toshi BLOBs daily) materialises,
the cost model breaks down — every full sweep would auto-promote a
large chunk of the corpus back to Frequent for 30 days, eroding the
saving. Worth a 6-month cost review to validate against actuals.

## Alternatives considered

### A. Standard everywhere (status quo)

No change. Continues paying ~$230/mo across toshi+ths source corpus.
**Rejected** because the cost is real and the alternatives have
acceptable risk.

### B. GIR @ 30d on source (mirror the backup-side policy)

Tempting symmetry: same lifecycle on source and backup. **Rejected**
because:

- Per-GB retrieval fee ($0.012/GB) is *not* covered by storage
  savings if any production process scans cold data. A single
  full-corpus re-analysis on 8 TB toshi would cost $98 in retrieval
  fees — small in absolute terms but indicative: the cost becomes
  *unpredictable* and tied to scientific-workflow decisions that are
  outside this system's control.
- Intelligent-Tiering provides equivalent storage savings with no
  retrieval-fee exposure.

### C. Intelligent-Tiering on all source buckets (including static-reports)

Apply uniformly. **Rejected** because static-reports' 40M-object
monitoring fee + 128 KB minimum-size tax inverts the savings —
costs ~$622/year more than Standard. Including it in scope to reduce
operational complexity costs more than the simplification is worth.

### D. Glacier Flexible Retrieval or Deep Archive on source

Cheapest storage ($0.0036 or $0.00099/GB/mo respectively). **Rejected**
because both require an explicit Restore (thaw) operation before reads,
which:

- Breaks production read paths (analyses can't wait 5–48h for thaw)
- Breaks the backup engine — `copy_object` against a Flexible/Deep
  Archive object returns `InvalidObjectState`. The backup engine has
  no thaw flow (same reason ADR-006 dropped Deep Archive from the
  backup-side policy).

### E. Sub-prefix lifecycle (e.g. only `/data/2024/` and older)

Apply Intelligent-Tiering only to clearly-archival prefixes within
each bucket. **Rejected as overkill** — the bucket-level lifecycle
already gets ~99% of the savings via auto-tiering, and prefix
discipline imposes ongoing cognitive load on whoever writes new data.

### F. Bundle small files into archives (for static-reports)

Pre-aggregate static-reports into quarterly tarballs to drop object
count below the monitoring-fee threshold. **Out of scope** — would
need a separate ADR + UX changes to how reports are browsed. Worth
revisiting if the static-reports corpus continues to grow and the
$70/mo Standard cost becomes material.

## Risks

### Backup-engine auto-promotion during initial sync

Intelligent-Tiering treats server-side `CopyObject` as an access
event, which auto-promotes the source object to Frequent Access for
30 days. For ongoing daily incremental backups this is a non-issue
because:

- Backup only copies *new* objects (those not yet in the backup
  bucket)
- "New" means recently-written → already in Frequent Access tier
  (haven't aged the 30 days needed to drop to IA)
- So the auto-promotion is a no-op in steady state

The exception is the **initial mass migration** — when we first apply
the lifecycle, AWS will tier the existing 11M objects (toshi+ths)
based on AWS's existing access logs. If a backup `--full-sync` runs
during that window, it would auto-promote all touched objects to
Frequent for 30 days, eroding ~30 days of Archive Instant savings.

**Mitigation:** apply the lifecycle change and let the tier-balance
settle (~7 days) before any deliberate full-sync activity. Daily
incremental backups during the settling window are fine.

### Cost-model drift over time

The 1/99 access assumption holds today. If a future workflow re-reads
old data systematically, savings degrade. **Mitigation:** schedule a
6-month cost review (~2026-12) to validate against AWS Cost Explorer.
If access patterns drift warm, re-evaluate scope.

### Inventory metadata growth

Adding the optional `IntelligentTieringAccessTier` field to the source
S3 Inventory config would let us track per-object tier distribution
over time. Useful for the cost review but not required for the basic
adoption. Defer to the implementation phase.

### Static-reports staying on Standard creates a documentation footnote

Operators may try to apply the same lifecycle uniformly. Mitigation:
runbook + this ADR document the exclusion + reasoning + the
40M-object monitoring threshold.

## Implementation scope

| Component | File / surface | Effort |
|---|---|---|
| Apply lifecycle to toshi-api-prod | Source-account `aws s3api put-bucket-lifecycle-configuration` | Trivial — one CLI command + verify |
| Apply lifecycle to ths-dataset-prod | Same | Trivial |
| Cross-reference from ADR-006 (which only addressed backup-side lifecycle) | `docs/design/adr/ADR-006-…` | Trivial |
| Update Cost Model doc with the new tier mix | `docs/architecture/cost-model.md` | Small |
| Cheatsheet entry: "applying source-bucket lifecycle" | `docs/operations/cheatsheet.md` | Small |
| 6-month cost-review reminder | Issue or calendar | Trivial |
| Verify backup pipeline unaffected via one production cycle | Wait 24h post-application; check next day's health report | Trivial |

### Roll-out sequence

1. Apply lifecycle to **ths-dataset-prod first** (smaller blast
   radius — 1 TB, 4M objects).
2. Wait ~7 days for tier balance to settle.
3. Confirm next health-report cycle shows no regression on ths
   (inventory_age stays fresh, divergence count = 0, restore tests
   pass).
4. Apply lifecycle to **toshi-api-prod**.
5. Wait another ~7 days for settling + first daily backup cycles.
6. Verify total cost trajectory in Cost Explorer at +30 days.

### Suggested lifecycle JSON (each source bucket)

```json
{
  "Rules": [
    {
      "ID": "IntelligentTiering",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "Transitions": [
        {"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}
      ]
    }
  ]
}
```

Apply via `aws s3api put-bucket-lifecycle-configuration` under the
source-account profile (`nshm-admin`), not the backup-account.

## Links

- [ADR-006: Backup-bucket lifecycle simplification](ADR-006-simplify-storage-tiers-drop-deep-archive.md) — sibling ADR for the backup side
- [Backup Solution Plan](../backup-solution-plan.md) — overall architecture; should gain a paragraph noting source-side tiering once this ADR is accepted
- [Cost Model](../../architecture/cost-model.md) — current cost breakdown; needs revision post-implementation
- [PROD-DEPLOY-LOG Step 18](../../PROD-DEPLOY-LOG.md) — backup-side migration record + the observation that two buckets had no lifecycle at all, which is what made the backup-side savings outsize the ADR-006 projection

## Open questions

- **Object-count growth trajectory** — toshi at 7M and growing. At what
  point does the monitoring fee start to dominate? Math: monitoring
  equals storage savings at ~40M objects on a 1/99 split (~$100/mo
  monitoring = ~$170/mo storage saved for 8 TB). Worth tracking.
- **Static-reports long-term strategy** — accepted as out-of-scope here,
  but the bucket is the largest object-count concern and likely to
  grow. A follow-up ADR considering pre-aggregation or archive-tier
  options may become worthwhile once the corpus exceeds (say) 50M
  objects or 5 TB.
