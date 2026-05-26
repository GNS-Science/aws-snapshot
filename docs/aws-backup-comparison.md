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

### What AWS Backup cannot detect

- **A backup that's silently missing source keys.** AWS Backup
  considers a snapshot complete if the job finishes without error;
  it does not compare contents to source. If a permissions glitch,
  bucket-policy mistake, or upstream bug causes 10 % of keys to be
  silently skipped, AWS Backup reports green forever.
- **A backup pipeline that has stopped delivering inventories.** If
  the underlying mechanism (in our case, S3 Inventory) goes dark,
  AWS Backup has no equivalent signal — the next failed job is the
  first you'd hear about it.
- **Intentional source deletion impact.** If a team deletes 6 TB of
  source data, AWS Backup keeps the snapshot indefinitely (or
  expires it per retention policy) — with no informational signal
  that source state has changed materially.
- **Backup-side drift from source intent.** Once a deletion-protected
  snapshot exists, AWS Backup gives you no way to see which keys it
  carries that no longer exist in source (class-2 orphans). They
  silently accrue cost without surfacing.

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
