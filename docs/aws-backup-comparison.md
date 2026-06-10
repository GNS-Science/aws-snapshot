# AWS Backup Comparison

A single-page reference for "why did we build something instead of using
AWS Backup?" Suitable to share with procurement, audit, an incoming
engineer, or anyone questioning the trade. Two arguments are made
together here because either alone is incomplete: **the cost saving
funds the engineering**; **the signal coverage justifies the
engineering**.

## TL;DR

| Axis | AWS Backup | This system |
|---|---|---|
| **Annual cost** (11.7 TB, 4 sources) | ~$20,400 NZD | ~$1,300 NZD (~16× cheaper) |
| **Source-vs-backup verification** | Job-level success only | Per-key divergence (both directions) via daily Athena scan |
| **Restore validation** | Boolean per job, no per-object check | Sampled restore + checksum, daily canary + rotation |
| **Pipeline freshness signal** | None — silence = "everything fine" | Class-3 yellow when inventory > 30h stale |
| **Source-deletion visibility** | Invisible | Class-2 informational, per source |
| **Backup-orphan visibility** | Invisible | Class-2 informational, per source |
| **Signal classification** | Binary success / fail | Class 1 / 2 / 3 — actionable separated from noteworthy |
| **Decision audit trail** | Console actions | ADRs, CHANGELOG, deploy log |
| **IAM transparency** | Service-managed role | Per-source reader vs restore roles, no-delete bucket policies + restore-test name-prefix exception |

## Why we're doing more than AWS Backup

Honest summary up front: this system does more than AWS Backup, and
that's deliberate scope expansion. The DR requirement is what pulled
it in. The chain of reasoning:

1. **Cost-efficient backup at 11.7 TB requires *incremental* sync**,
   not full snapshots. Full-snapshot daily would multiply storage by
   ~50× and turn the cost story upside-down.
2. **Incremental sync needs source-vs-backup diffing** — you have to
   know which keys are new or changed before you can copy "only the
   delta." We built that diff infrastructure on S3 Inventory + Athena
   because we'd need it for cost reasons regardless of any other
   feature.
3. **Once the diff infrastructure exists, surfacing it as signal
   coverage is nearly-free incremental work** — the same queries
   that drive incremental sync can also report `source - backup` and
   `backup - source` counts. The signals aren't separate scope;
   they're the diff infrastructure made visible.
4. **And once we have measurable diff, the DR plan becomes
   defensible** — "the backup is correct" stops being a claim of
   trust ("the daily job succeeded") and becomes a claim of evidence
   ("we measure the gap, and the measurement says zero").

So the comparison below has two halves not because they're separate
arguments, but because **the diff infrastructure that makes the cost
half work is also what makes the signal-coverage half possible**.
Without incremental backup, no diff. Without diff, no measurable DR
verification. They're the same engineering investment seen from two
angles.

## Cost — the easy half

AWS Backup baseline (2026-04, pre-migration): ~$1,700 NZD/month → $20,400/year.
Custom solution: ~$108 NZD/month → ~$1,300/year. **Annual saving ~$19,000 NZD.**

The saving funds the engineering effort and then some. See
[backup-solution-plan.md](design/backup-solution-plan.md) for the
itemised model and [ADR-006](design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
for the storage-tier trade.

## Signal coverage — the harder half

AWS Backup tells you "your job succeeded." It does not tell you
*whether the backup contains what the source contained*. That gap is
the one this system closes — and it's the one that matters during a
real disaster.

### What's outside AWS Backup's contract — signals only the data owner can verify

AWS Backup is well-engineered software built by AWS engineers who
understand the storage layer intimately. We're not claiming it's
broken or inadequate at what it does. Its contract is well-defined:

> *"We copied what was visible to us at scan time, using your
> configured IAM grants, in the storage classes you specified."*

That contract is honoured robustly. The signals below check a
**different question** — *"does what was visible at scan time match
what the application considers correct?"* — which AWS Backup cannot
answer for us because only the data owner knows the domain semantics
that determine "correct." None of these failure modes are bugs in
AWS Backup; they're domain-knowledge gaps that any general-purpose
snapshot service has to leave to the data owner to close.

- **A backup that's silently missing source keys.** Permissions
  glitches, bucket-policy conditions, KMS access issues, or upstream
  processes that fail to deposit some files produce a snapshot that
  is *internally complete* (the job finished successfully) but
  *application-incomplete* (10 % of what should be there isn't).
  AWS Backup correctly reports green — the job did succeed for what
  it could see — but we'd want to know about the gap. Our
  `divergence_counts` Athena query compares against an independent
  source inventory and surfaces a class-1 RED when keys go missing.
- **A backup pipeline that has stopped delivering inventories.**
  This one is *our-architecture-specific*: we use S3 Inventory as a
  signal channel; AWS Backup doesn't use the same channel. So this
  "gap" is partly self-inflicted by our chosen design. Listed for
  completeness rather than as a critique of AWS Backup.
- **Intentional source deletion impact.** When a team deletes 6 TB
  of source data, AWS Backup faithfully captures the post-delete
  state (or keeps a pre-delete snapshot per retention) — neither
  outcome is "wrong," both are decisions for the data owner. Our
  count_delta signal surfaces *that* the source change happened so
  an operator can confirm intent rather than wonder if a bug caused
  the discrepancy.
- **Backup-side drift from source intent.** Once a deletion-protected
  snapshot exists, the backup retains keys that no longer exist in
  source. This is by design (we *want* deletion-protection), but the
  data owner needs visibility to decide whether to purge the orphans
  or let them age out. Our class-2 ℹ orphan signal makes that drift
  visible — not because AWS Backup should automatically clean it up,
  but because that decision belongs to the data owner.

### What this system detects

ADR-009 formalises three signal classes:

| Class | Glyph | Meaning | Example |
|---|---|---|---|
| **1** | ⚠ red | The backup system has actually failed | "backup is missing 1,247 source keys" |
| **2** | ℹ info | Noteworthy but expected; no action required | "backup has 89 orphans (source deletions retained per ADR-006)" |
| **3** | ⚠ yellow | Operationally degraded but not failing yet | "inventory > 30h stale" |

The classifier (`src/nzshm_backup/health_report.py`) drives the daily
Slack + email report; each class renders distinctly so operators don't
chase informational lines.

See [Health Report user guide](user-guide/health-report.md) for the
operator-facing description and [ADR-009](design/adr/ADR-009-health-check-measurement-model.md)
for the design rationale.

## What you give up with the custom system

To be fair to AWS Backup, these are the real costs of going custom:

- **Engineering ownership.** No vendor on-call. When something
  breaks, GNS owns the fix. Mitigated by ~414 unit tests and an
  ops cheatsheet, but real.
- **Configuration surface area.** There's a YAML, an SSM-pushed
  config, an EventBridge schedule, an SNS topic, two Slack secrets,
  cross-account IAM roles. AWS Backup is one console form per source.
- **Skill dependency.** Bus factor: someone has to understand Athena
  + S3 Inventory + Pydantic + Typer + Serverless. AWS Backup needs
  one IAM role and tagging policy.
- **Single AWS Organization design.** The current architecture
  assumes Org-scoped accounts (SSO, IAM federation). Standalone
  accounts would work but require manual IAM-user provisioning.

## When AWS Backup IS the right choice

For honesty: AWS Backup is genuinely the right tool when:

- The data corpus is < ~1 TB (storage savings don't dominate
  engineering cost).
- The team has no AWS-native engineering capacity to maintain
  Serverless + Lambda + Athena + IAM.
- Compliance requires AWS-managed encryption keys and audit logs
  exclusively, with no custom path acceptable.
- The data is in services AWS Backup natively supports and a
  third-party diff/verification tier isn't a requirement.

NSHM's case sits firmly in the "custom wins" quadrant: 11.7 TB and
growing, scientific-correctness requirements (data integrity is the
whole point of having a backup), and an engineering team capable of
maintaining the stack.

## Links

- [Backup Solution Plan](design/backup-solution-plan.md) — full
  architecture and cost model
- [ADR-006: Storage tiers](design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)
- [ADR-009: Signal taxonomy](design/adr/ADR-009-health-check-measurement-model.md)
- [Daily Health Report](user-guide/health-report.md) — operator view of the signals
- [Production Deploy Log](PROD-DEPLOY-LOG.md) — chronological evidence of the system in production use
