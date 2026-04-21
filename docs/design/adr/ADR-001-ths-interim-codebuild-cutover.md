# ADR-001: THS interim CodeBuild cutover

- Status: Accepted
- Date: 2026-04-21

## Context

THS scheduled backups were failing in the Lambda path before S3 Batch submission.
Manifest preparation (source+backup listing and diff) exceeded practical Lambda
runtime/memory behavior for THS scale.

## Decision

Use EventBridge -> CodeBuild for THS scheduled execution as an interim
reliability path.

- Keep command surface unchanged: CodeBuild runs `backup run --source ths`.
- Keep S3 Batch submission contract unchanged.
- Keep existing backup buckets and object semantics unchanged.

## Alternatives Considered

1. Increase Lambda memory/timeout only.
2. Keep Lambda and accept intermittent failures/timeouts.
3. Immediate switch to full inventory architecture before restoring reliability.

## Consequences

- Restores operational reliability for THS now.
- Adds build artifact/update operational overhead.
- Does not, by itself, solve long-term prep efficiency and memory growth risks.

## Links

- Epic: https://github.com/GNS-Science/nzshm-backup/issues/8
- THS cutover: https://github.com/GNS-Science/nzshm-backup/issues/9
- Manifest bottleneck doc: ../S3_MANIFEST_BOTTLENECK.md
