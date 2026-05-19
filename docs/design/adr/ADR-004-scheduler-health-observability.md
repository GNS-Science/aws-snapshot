# ADR-004: Scheduler health observability

- Status: Accepted
- Date: 2026-04-21

## Context

`backup status` reports backup pipeline state after submit, but does not directly
show scheduler trigger health. During long prepare phases this can look like no
progress even when EventBridge and CodeBuild are working correctly.

## Decision

Add `backup schedule health` as an operator view combining:

- EventBridge rule state and target wiring
- EventBridge invocation and failed-invocation metrics over a lookback window
- latest CodeBuild build status for codebuild-targeted rules

## Alternatives Considered

1. Keep using separate AWS CLI commands manually.
2. Extend `backup status` to include scheduler metrics.

## Consequences

- Faster triage of schedule-vs-run failures.
- Clear pre-submit visibility for long-running prepare phases.
- Slightly broader schedule command surface area.

## Links

- THS cutover issue: https://github.com/GNS-Science/nzshm-backup/issues/9
- Scheduling guide: ../../user-guide/scheduling.md
