# ADR-003: Run-state transition model

- Status: Proposed
- Date: 2026-04-21

## Context

Operators need visibility across scheduler, build, manifest prep, and batch
execution phases. Existing `_state/last-run.json` provides useful compatibility
but needs an explicit phase model for consistency across paths.

## Decision

Adopt and document a phase model:

- `running`
- `preparing_manifest`
- `prepared`
- `submitted`
- `active` (derived from S3 Batch status when `batch_job_id` exists)
- terminal: `completed`, `failed`, `skipped`

Transition strategy:

- Keep writing `_state/last-run.json` for backward compatibility.
- Evolve toward centralized run-state storage for cross-source reporting.

## Alternatives Considered

1. Keep ad-hoc statuses without explicit transitions.
2. Move directly to centralized state only and drop bucket-local state.

## Consequences

- Improves debugging and operator clarity during long prep windows.
- Supports both current and future execution backends.
- Adds a small amount of state-model maintenance overhead.

## Links

- Run state transition issue: https://github.com/GNS-Science/nzshm-backup/issues/11
- Manifest bottleneck doc: ../S3_MANIFEST_BOTTLENECK.md
