# ADR-002: Inventory-based manifest pipeline for THS

- Status: Implemented
- Date: 2026-04-21
- Updated: 2026-05-04

> **2026-06-27 framing note — see [ADR-014](ADR-014-inventory-optional-health-signals.md).**
> ADR-002 established S3 Inventory as the manifest mechanism for THS,
> where scale (millions of objects) made `ListObjectsV2`-based manifest
> building prohibitive. ADR-014 frames Inventory more generally as a
> scale-appropriate verification mechanism — not a universal one —
> and introduces `inventory_enabled: false` as a first-class operating
> mode for installs below that scale. ADR-002's design is unchanged
> for installs above it.

## Context

Current THS manifest prep uses live source+backup listing and in-process diff.
This scales poorly as backup object counts grow and has produced timeout/OOM
failure modes. Even successful CodeBuild prep runs are long and variable.

Observed in production testing:

- Lambda path timed out before submit for THS-scale object counts.
- CodeBuild can complete, but prep is still long (~50-60 minutes) and can fail
  at smaller compute sizes when reconciliation set is large.
- As backup buckets grow, live-list diff pressure increases (source + backup
  object catalogs both need to be loaded/compared).

The project must preserve backup semantics (explicit cadence, no delete
propagation, anti-poisoning posture) while improving prep reliability and cost.

## Decision

Implement an inventory-based `prepare -> submit` pipeline for THS while keeping
the external run UX unchanged.

- One-time setup enables daily S3 Inventory (source and backup).
- Per run:
  1. `prepare`: inventory diff query (Athena) -> manifest CSV + ETag
  2. `submit`: S3 Batch `CreateJob` from that manifest
- Keep backup bucket naming and data semantics unchanged.

### Detailed design choices

1. **Run interface remains stable**
   - Operator/scheduler command stays `backup run --source ths`.
   - Internal execution mode is selected by source config (`inline` vs
     `inventory`).

2. **Inventory inputs**
   - Use daily Parquet S3 Inventory for source and backup buckets.
   - Store inventory outputs in a dedicated control bucket/prefix (not backup
     data/object namespace).

3. **Diff semantics (v2 target)**
   - Compare source and backup by key, size, etag.
   - Include object when backup key is missing or key exists but size/etag differ.
   - Ignore operational prefixes in backup-side datasets.

4. **Manifest format**
   - Emit S3 Batch CSV rows as `source-bucket,url_encoded_key`.
   - Keep `/` unescaped in keys.

5. **State model**
   - Persist/emit run phases: `running`, `preparing_manifest`, `prepared`,
     `submitted`, terminal (`completed`, `failed`, `skipped`).
   - Derive `active` from S3 Batch `DescribeJob` when `batch_job_id` exists.

6. **Scheduler/target compatibility**
   - Continue using EventBridge -> CodeBuild for THS during pilot.
   - Inventory pipeline runs inside the same scheduled entrypoint.

## Non-goals (for this ADR)

- Replacing S3 Batch submission contract.
- Implementing centralized run-state storage in this phase.

## Rollout plan

1. ~~Add inventory mode behind per-source config flag.~~ Done 2026-04-23.
2. ~~Enable for THS only in production.~~ Done 2026-04-23.
3. Keep inline mode available as rollback path.
4. ~~Validate 2 consecutive scheduled THS runs.~~ Done 2026-04-23.
5. ~~Promote to other large sources.~~ Enabled for toshi, weka (2026-04-23),
   static (2026-05-04). All sources now use inventory mode on Lambda.
6. ~~Manifest materialization pivot to Athena UNLOAD.~~ Lambda streaming
   OOM'd for static (~40M objects). Replaced with Athena UNLOAD +
   S3 multipart-copy concat (2026-05-04). See ATHENA_MANIFEST_PIPELINE.md v2.
7. ~~CodeBuild cutover for THS reversed.~~ All sources back on Lambda
   (2026-05-04) — UNLOAD eliminates the need for CodeBuild compute.

## Acceptance criteria

1. THS scheduled runs no longer perform live source+backup full listing in the
   runtime path.
2. THS runs submit S3 Batch from inventory-derived manifests successfully.
3. Two consecutive scheduled THS runs complete end-to-end.
4. Prep phase reliability and cost are improved versus current full-listing
   CodeBuild path.
5. No backup bucket migration is required.

## Alternatives Considered

1. Continue CodeBuild full live-list diff path only.
2. Source-only inventory manifests (copy all source objects each run).
3. S3 replication model.

## Consequences

- Expected lower and more stable prep cost/runtime than full live listing.
- Daily snapshot lag is introduced by inventory cadence.
- Requires inventory setup and Athena metadata/query operations.
- Adds inventory freshness/partition management responsibilities.
- Keeps existing scheduler/operator UX stable, minimizing operational retraining.

## Risks and mitigations

- **Inventory freshness lag**
  - Mitigation: explicit snapshot selection policy; document expected RPO impact.
- **Athena query/schema drift**
  - Mitigation: versioned DDL/query templates; test against canary source first.
- **Manifest encoding mismatches**
  - Mitigation: enforce URL-encoded key output and test against known reserved
    character keys.
- **Operational complexity growth**
  - Mitigation: keep setup/run command surface minimal and retain rollback switch
    to inline mode.

## Inventory freshness and effective backup timestamp

In inventory mode, the effective data timestamp of each backup run is determined by
the inventory snapshots used during `prepare` (source + backup), not by backup run
start time.

Operational rule:

- Record `source_inventory_dt` and `backup_inventory_dt` for every run.
- Treat the effective backup timestamp as `min(source_inventory_dt, backup_inventory_dt)`.

### Freshness lag

Inventory is snapshot-based and generated on a schedule (typically daily), so there
is a lag between source writes and visibility in inventory-derived manifests.

Implications:

- Objects written after the selected source inventory snapshot are not included in
  that run’s manifest.
- They are picked up in a later run once a newer inventory snapshot is available.

### Ballpark timing and cost

These are order-of-magnitude planning estimates for THS-scale catalogs:

- Inventory availability:
  - first inventory after enablement: often up to 24-48h
  - ongoing daily inventory: typically available within hours of generation window
- Inventory listing cost:
  - roughly `$0.0025 USD / 1M objects listed`
  - for ~7.8M listed objects (source + backup): ~`$0.02 USD` (~`NZD 0.03-0.04`) per run
- Athena diff query cost (Parquet, partition-pruned):
  - typical scan ~1-5 GB
  - roughly `NZD 0.01-0.04` per run

Total inventory+query prep path is generally expected to be in the low-cent range
per run, materially below long CodeBuild full-listing prep runs.

## Links

- Epic: https://github.com/GNS-Science/nzshm-backup/issues/8
- Inventory pilot: https://github.com/GNS-Science/nzshm-backup/issues/12
- Manifest bottleneck doc: ../S3_MANIFEST_BOTTLENECK.md
- Athena implementation design: ../ATHENA_MANIFEST_PIPELINE.md
- Cost model: ../../architecture/cost-model.md
