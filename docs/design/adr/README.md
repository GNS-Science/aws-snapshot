# Architecture Decision Records

This directory stores Architecture Decision Records (ADRs) for major design
choices in the backup system.

## ADR Index

- [ADR-001: THS interim CodeBuild cutover](ADR-001-ths-interim-codebuild-cutover.md)
- [ADR-002: Inventory-based manifest pipeline for THS](ADR-002-inventory-manifest-pipeline-ths.md)
- [ADR-003: Run-state transition model](ADR-003-run-state-transition-model.md)
- [ADR-004: Scheduler health observability](ADR-004-scheduler-health-observability.md)
- [ADR-005: Daily health report (slow-path observability)](ADR-005-weekly-health-report.md)
- [ADR-006: Simplify backup-bucket lifecycle (drop Deep Archive)](ADR-006-simplify-storage-tiers-drop-deep-archive.md)
- [ADR-007: Harden inventory control-plane bucket](ADR-007-harden-inventory-control-plane-bucket.md)
- [ADR-008: Notification recipients managed from YAML](ADR-008-notification-recipients-managed-from-yaml.md)
- [ADR-009: Health-check signal-class taxonomy](ADR-009-health-check-measurement-model.md)
- [ADR-010: Source-bucket Intelligent-Tiering (toshi + ths only)](ADR-010-source-bucket-intelligent-tiering.md) — **Proposed**
- [ADR-011: Four-colour signal taxonomy (blue / green / amber / red)](ADR-011-four-color-signal-taxonomy.md) — **Proposed** (partially supersedes ADR-009)
- [ADR-012: GitHub Actions deployment workflow (OIDC + tag-trigger + manual approval)](ADR-012-github-actions-deploy-workflow.md) — **Proposed**

## ADR Template

- Title
- Status (`Proposed`, `Accepted`, `Superseded`)
- Date
- Context
- Decision
- Alternatives Considered
- Consequences
- Links
