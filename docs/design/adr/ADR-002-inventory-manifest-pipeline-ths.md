# ADR-002: Inventory-based manifest pipeline for THS

- Status: Proposed
- Date: 2026-04-21

## Context

Current THS manifest prep uses live source+backup listing and in-process diff.
This scales poorly as backup object counts grow and has produced timeout/OOM
failure modes. Even successful CodeBuild prep runs are long and variable.

## Decision

Implement an inventory-based `prepare -> submit` pipeline for THS while keeping
the external run UX unchanged.

- One-time setup enables daily S3 Inventory (source and backup).
- Per run:
  1. `prepare`: inventory diff query (Athena) -> manifest CSV + ETag
  2. `submit`: S3 Batch `CreateJob` from that manifest
- Keep backup bucket naming and data semantics unchanged.

## Alternatives Considered

1. Continue CodeBuild full live-list diff path only.
2. Source-only inventory manifests (copy all source objects each run).
3. S3 replication model.

## Consequences

- Expected lower and more stable prep cost/runtime than full live listing.
- Daily snapshot lag is introduced by inventory cadence.
- Requires inventory setup and Athena metadata/query operations.

## Links

- Epic: https://github.com/GNS-Science/nzshm-backup/issues/8
- Inventory pilot: https://github.com/GNS-Science/nzshm-backup/issues/12
- Manifest bottleneck doc: ../S3_MANIFEST_BOTTLENECK.md
- Cost model: ../../architecture/cost-model.md
