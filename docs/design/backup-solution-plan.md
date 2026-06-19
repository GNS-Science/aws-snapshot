# NSHM Backup Solution - Design Plan

## Executive Summary

**Goal:** Replace expensive AWS Backup solution ($1,700 NZD/month = $20,400 NZD/year) with custom AWS-native solution reducing costs by 64% while maintaining coverage for human error and infrastructure risks.

**Target Architecture:** Serverless Python/Click CLI running on AWS Lambda with S3 Glacier integration.

**Projected Savings:** $13,080 NZD/year (from $20,400 to $7,320)

---

## Data Sources

| Source alias | Resource | Size | Storage Type | Backup Method |
|--------|------|------|--------------|---------------|
| `toshi` | ToshiFileObject-PROD | 2.3 GB | DynamoDB | Point-in-Time Export to S3 |
| `toshi` | ToshiIdentity-PROD | — | DynamoDB | Point-in-Time Export to S3 |
| `toshi` | ToshiTableObject-PROD | — | DynamoDB | Point-in-Time Export to S3 |
| `toshi` | ToshiThingObject-PROD | 16 GB | DynamoDB | Point-in-Time Export to S3 |
| `toshi` | nzshm22-toshi-api-prod | 8 TB | S3 | S3 Batch Operations |
| `ths` | ths-dataset-prod | 1 TB | S3 | S3 Batch Operations |
| `static` | nzshm22-static-reports | 2.7 TB | S3 | S3 Batch Operations |
| `weka` | nzshm22-weka-ui-prod | 80 MB | S3 | Incremental sync |
| **Total** | | **~11.7 TB + 18.3 GB** | | |

All sources are cross-account: source account `461564345538` → backup account `737696831915`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   AWS EventBridge                           │
│                 (Cron: weekly/daily)                        │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    AWS Lambda                               │
│              (nzshm-backup CLI)                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  • Schedule orchestration                            │   │
│  │  • S3 → S3 Glacier transition                        │   │
│  │  • DynamoDB → S3 export                              │   │
│  │  • Pruning/retention policies                        │   │
│  │  • Cost tracking & reporting                         │   │
│  │  • Email notifications (SES/Slack)                   │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
    ┌────────┐   ┌─────────┐   ┌──────────┐
    │  S3    │   │  S3     │   │ DynamoDB │
    │ Source │   │ Glacier │   │  Export  │
    │ Buckets│   │ Storage │   │  to S3   │
    └────────┘   └─────────┘   └──────────┘
```

---

## Storage Tiers & Retention Policy

| Tier | Duration | Storage Type | Cost (NZD/GB-month) | Access Time |
|------|----------|--------------|---------------------|-------------|
| Hot | 0-30 days | S3 Standard | $0.036 | Immediate |
| Cold | 30+ days (forever) | S3 Glacier Instant | $0.007 | Milliseconds |

Backup objects are never expired (see
[ADR-006](adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md) — dropped
the original Deep Archive tier and the 365-day expiry to remove the silent
annual re-copy and the unimplemented thaw flow). Superseded (non-current)
versions are pruned separately via `NoncurrentVersionExpiration`.

**Note:** Starting with single-region (ap-southeast-2 Sydney). Cross-region can be added later if compliance requires.

---

## Scheduling System

**Frequency Options:**
- **Standard:** Weekly backups (Sunday 2:00 AM NZST)
- **Active Experiment Mode:** Daily backups (configurable period during sensitivity testing)
- **Manual Trigger:** On-demand via CLI flag or EventBridge rule

**Pruning Strategy:**
- Automated lifecycle policies transition objects between tiers based on age
- Custom pruning logic removes backups older than retention policy
- Dry-run mode previews actions before execution

---

## Backup Methods

### Backup semantics (explicitly not replication)

This project is intentionally designed as a **backup system**, not a source-mirroring
replication system.

Core semantics:

1. **Explicit run cadence** (weekly by default, optionally daily during active periods)
   creates discrete, auditable recovery checkpoints.
2. **No delete propagation** from source to backup. Source deletions remain recoverable
   from backup buckets indefinitely; intentional purging is an out-of-band admin task
   (manual-purge runbook, tracked under #23).
3. **Anti-poisoning posture**: backup buckets use versioning + non-current retention,
   so source mutations do not irreversibly destroy the last known-good backup copy.

Why this matters:

- A pure replication model prioritizes source parity, which can propagate bad changes
  quickly (corruption, accidental overwrite, or malicious mutation).
- This project prioritizes recoverability and controlled retention over mirror fidelity.

Cross-region replication may still be used as a storage/DR transport mechanism in future,
but it is not the primary backup model for NSHM data protection.

### S3 Backup (ToshiBucket + THS_dataset_prod)

**Recommended:** Same-region backup with S3 Lifecycle policies

| Method | Pros | Cons | Cost |
|--------|------|------|------|
| S3 Lifecycle + Copy | Cheap, simple, native | No regional failover | $ |
| Cross-Region Replication | Automatic, failover | Higher egress costs | $$ |
| AWS Backup (current) | Managed service | Expensive overhead | $$$ |

### DynamoDB Backup (ToshiAPI Tables)

**Recommended:** DynamoDB Point-in-Time Export to S3

| Method | Pros | Cons | Cost |
|--------|------|------|------|
| PIT Export to S3 | Native, consistent, cheap | Export time varies | $ |
| AWS Backup | Centralized management | Higher service costs | $$ |
| Custom scan + JSON/CSV | Full control | Complex, slow, RCU costs | $$ |

---

## Cost Analysis

For current storage tier pricing, monthly cost modelling, and AWS Backup comparison see
[Cost Model](../architecture/cost-model.md).

### Summary

- **Current (AWS Backup):** $1,700 NZD/month ($20,400/year)
- **Custom solution (steady-state, aged):** ~$108 NZD/month (~$1,300/year)
- **Savings:** ~94% reduction once the 11.7 TB corpus has aged into Glacier
  Instant Retrieval (per [ADR-006](adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md);
  previously projected ~97% with the discontinued Deep Archive tier)

---

## CLI Tool Design

### Command Structure (as implemented)

```bash
# Manual backup trigger
$ backup run --source arkivalist          # Run backup for one source
$ backup run --source arkivalist --dry-run

# Backup status
$ backup status --source arkivalist       # Last run, per-bucket/table state
$ backup status --source arkivalist --format json

# Schedule management
$ backup schedule show --source arkivalist
$ backup schedule add --source arkivalist --frequency weekly
$ backup schedule remove --source arkivalist

# Restore — DynamoDB (async, submit-and-return)
$ backup restore run --source arkivalist \
    --tables arkivalist-api-dev-events \
    --to-point-in-time 2026-03-15T09:00:00Z
$ backup restore run --source arkivalist --no-pitr  # skip auto PITR re-enable

# Restore — S3
# Currently: direct copy_object (suitable for current small buckets)
# Planned: S3 Batch Operations for all sizes (see docs/design/s3-restore-strategy.md)
$ backup restore run --source arkivalist --buckets my-bucket
$ backup restore run --source arkivalist --buckets my-bucket --prefix models/2026/

# Restore status
$ backup restore status --source arkivalist
$ backup restore status --source arkivalist --tables arkivalist-api-dev-events

# Testing & validation
$ backup test restore --source arkivalist            # Sample objects from each backup bucket (direct copy)
$ backup test restore --source arkivalist --use-batch  # Same, but via S3 Batch (tests IAM + Batch path)
$ backup test restore --source arkivalist --tables   # DynamoDB round-trip (submit + wait + verify)
$ backup test integrity --source arkivalist          # ETag + object count verification

# Configuration
$ backup config show
$ backup config push    # Write config to SSM Parameter Store (for Lambda)
$ backup config pull    # Read config from SSM
```

### Key Features
- Configuration via YAML/JSON file (version controlled)
- Dry-run mode for all operations
- JSON output option for scripting (`--output json`)
- Verbose logging to CloudWatch
- Cost approval workflow (auto-approve under $100, manual above)

---

## Restore Functionality

### Restore Considerations by Storage Tier

| Storage Tier | Retrieval Time | Retrieval Cost | Use Case |
|--------------|----------------|----------------|----------|
| S3 Standard (0-30 days) | Immediate | Free | Routine restores, testing |
| S3 Glacier Instant (30+ days, forever) | Milliseconds | $0.079/GB | All historical restores including DR |

### Restore Destinations

**Default Strategy:** Create temporary restore buckets with auto-cleanup
- Pattern: `nzshm-restore-{source}-{date}-{random}`
- Auto-delete after 7 days (configurable)
- Option to promote to permanent bucket

**DynamoDB Restore:** Always to new table (safer)
- Pattern: `{original-table}-restore-{date}`
- Manual copy-back to original if required
- Prevents accidental overwrites

### Approval Workflow

| Retrieval Cost | Approval Required |
|----------------|-------------------|
| < $100 NZD | Auto-approve |
| $100 - $500 NZD | Email approval (one admin) |
| > $500 NZD | Dual approval (two admins) |

---

## Testing & Validation

### Automated Test Schedule

| Test | Frequency | Scope | Success Criteria |
|------|-----------|-------|------------------|
| Small restore test | Weekly | 100 MB sample | Objects match source, checksums valid |
| DynamoDB table restore | Monthly | FileTable (2.3 GB) | Item count matches, sample records valid |
| Full S3 prefix restore | Monthly | 10 GB subset | All objects accessible, metadata intact |
| Full disaster recovery | Quarterly | Complete ToshiBucket | Full restore to isolated environment |
| Cross-account restore | Bi-annually | Small dataset | IAM roles work, data accessible |

### Validation Checks

1. **Object count verification:** Compare source vs restored object counts
2. **Checksum validation:** SHA-256 hash comparison (optional, slower)
3. **Sample record validation:** Random sampling of DynamoDB records
4. **Metadata preservation:** Content-Type, custom metadata intact
5. **Accessibility test:** Random object downloads succeed

### Test Output

- JSON test results for CI/CD integration
- HTML/PDF compliance reports for audits
- Slack/email notifications on test completion
- CloudWatch Logs for troubleshooting

---

## Notifications

### Email vs Slack Comparison

| Feature | AWS SES | Slack Integration |
|---------|---------|-------------------|
| Cost | $0.10/1000 emails | Free (existing workspace) |
| Setup | Domain verification | OAuth webhook |
| Reliability | High (AWS native) | High |
| Engagement | Email inbox | Real-time chat |
| Best For | Audit trail, compliance | Quick alerts, team visibility |

**Recommendation:** Use both - SES for formal notifications (completion reports, monthly summaries), Slack for immediate alerts (failures, critical errors).

### Notification Events

| Event | Channel | Priority |
|-------|---------|----------|
| Backup completed successfully | SES + Slack | Info |
| Backup failed | Slack immediate + SES detailed | Critical |
| Pruning executed | SES weekly summary | Info |
| Monthly cost report | SES only | Info |
| Schedule changed | SES + Slack | Info |
| Restore initiated | Slack + SES | Warning |
| Restore completed | SES + Slack | Info |
| Large restore approval needed | SES (approvers only) | Warning |

---

## Cost Tracking & Reporting

### Built-in Cost Features

```bash
$ backup costs predict --current 20400 --target 7420
$ backup costs report --last-month
$ backup costs breakdown --by-source
$ backup costs export --format csv --output-to s3://finance-reports/
```

### Metrics Tracked

- S3 Standard storage (GB-month)
- S3 Glacier storage (GB-month)
- DynamoDB exports (count + size)
- Lambda compute (GB-seconds)
- SES/Slack notifications (count)
- Data transfer (if any)
- Glacier retrieval fees (separate line item)

### Reporting Output

- Monthly cost summary (JSON + human-readable)
- Cost per backup job
- Trend analysis (month-over-month)
- Budget alerts (via AWS Budgets integration)
- Finance system export (CSV)

---

## Implementation Phases

### Phase 1: Foundation

#### Step 1: CLI Skeleton (Week 1) ✅ Complete
- [x] CLI skeleton with Typer (chose Typer over Click for type safety)
- [x] All subcommand groups registered (schedule, run, restore, test, status, report, costs, config)
- [x] State management for global flags (--verbose, --dry-run, --output)
- [x] Basic test infrastructure with pytest + moto

#### Step 2: Config + S3 Backup (Week 2) ✅ Complete
- [x] Configuration system with Pydantic models
- [x] YAML config loader with validation
- [x] Alias→ARN mapping for sources
- [x] S3 backup module with incremental sync (hybrid approach)
- [x] Lifecycle policy attachment (single transition at day 30 to Glacier IR, per ADR-006)
- [x] Globally unique backup bucket naming: `{bucket}-backup-{region}-{account_id}`
- [x] Delete protection via IAM (no s3:DeleteObject permission)
- [x] CloudWatch-compatible logging (JSON format option)
- [x] Lambda handler with Pydantic task schema
- [x] Serverless Framework config (serverless.yml)
- [x] Test suite: 35 tests, 71% coverage
- [x] All lint checks passing (ruff + black)

### Phase 2: DynamoDB + Scheduling (Week 3) ✅ Complete

- [x] DynamoDB export integration (PITR export to S3)
- [x] EventBridge scheduling rules (`schedule add/remove`, `hourly`/`minutely` frequencies)
- [x] Lambda function deployed and verified in sandbox (Serverless Framework v4)
- [x] IAM roles and policies (s3control, iam:PassRole for S3 Batch)
- [x] S3 Batch Operations for large buckets (`use_s3_batch` config flag, `s3_batch.py`)
- [x] Manual vs scheduled backup conflict resolved (ManagedBy tag ownership check)
- [x] Sandbox demo tooling (`scripts/sandbox_setup.sh`, `docs/sandbox-demo.md`)
- [x] 69 tests passing

**Production status:** S3 Batch role created, all sources configured with
`use_s3_batch: true` and `batch_manifest_mode: inventory`. Done.

### Phase 3: Notifications + Reporting

- [ ] SES email integration
- [ ] Slack webhook integration
- [ ] Cost tracking implementation
- [ ] Budget alerts setup
- [ ] CloudWatch Lambda error alarm → SNS → Slack + email (see ADR-005, fast path)
- [ ] Automated daily health report (see ADR-005, slow path)

### Phase 4: Restore Functionality ✅ Substantially complete

- [x] S3 restore — direct object copy (`s3_restore.py`, `backup restore run --buckets`)
- [x] DynamoDB PITR restore — submit-and-return (`backup restore run --tables --to-point-in-time`)
- [x] `backup restore status` — live table status polling
- [x] `--no-pitr` flag for short-lived test restores
- [x] Automatic PITR re-enable via `pitr-watcher` Lambda (SSM-based, EventBridge rule lifecycle)
- [x] Cross-account restore role (`nzshm-backup-restore`) — separate from reader role
- [x] Informational tags applied to restored tables (`RestoredBy`, `RestoredFrom`, `RestoredAt`)
- [x] Verified end-to-end against Arkivalist (cross-account, ap-southeast-2)
- [x] S3 Batch Operations for `restore run` — default when `s3_batch_role_arn` configured; direct `copy_object` fallback otherwise
- [x] `--use-batch` flag for `backup test restore` — exercises S3 Batch path for sampled objects
- [ ] DynamoDB `import-table` from S3 export (PITR > 35 days fallback — manual CLI only)

### Phase 5: Testing & Validation ✅ Substantially complete

- [x] `backup test restore` — inventory-based random sampling (Athena `ORDER BY RAND()`), CRC64NVME checksum verification, falls back to smart ETag
- [x] `backup test integrity` — three-tier verification (checksum → smart ETag → skip multipart), DynamoDB PITR + export checks. Weka used as canary for independent pipeline audit.
- [x] S3 versioning on backup buckets (`version_retention_days` config)
- [x] Last-run state persisted to `_state/last-run.json` in backup bucket
- [x] Inventory guards — refuse listing fallback for large inventory-mode sources
- [x] Validation strategy documented (`docs/design/VALIDATION_STRATEGY.md`)
- [ ] Test scheduling (EventBridge-triggered automated test runs) — see ADR-005
- [ ] Compliance reporting (HTML/PDF)
- [ ] `test full-drill` — quarterly DR exercise

### Phase 6: Parallel Run + Cutover ✅ Substantially complete

- [x] Arkivalist S3 buckets and DynamoDB tables configured
- [x] Cross-account session support (`get_cross_account_session`)
- [x] IAM roles created in Arkivalist account (`nzshm-backup-reader`, `nzshm-backup-restore`)
- [x] Full backup→restore→validate cycle verified on Arkivalist (S3 + DynamoDB)
- [x] Apply cross-account pattern to NSHM production (`461564345538`) — all 4 sources configured
- [x] IAM roles created in source account (`nzshm-backup-reader`, `nzshm-backup-restore`)
- [x] Lambda deployed to backup account (`737696831915`), config pushed to SSM
- [x] Pre-flight `backup check` command verified against all sources
- [x] Athena UNLOAD manifest pipeline — all sources on Lambda (28s for 40M objects)
- [x] Smart ETag comparison — eliminates false-positive re-copies from multipart uploads
- [x] All 4 sources backed up and object-count reconciled (50.8M objects, 11 TB)
- [x] Daily schedules live (13:05 NZST) — 7+ consecutive days clean as of 2026-05-14
- [x] Restore tests passing for all sources (checksum verified)
- [x] `.env` support for CLI convenience (python-dotenv)
- [ ] Restore drill against NSHM production data (`test full-drill`)
- [ ] Cost verification after first month (scheduled June 2026)
- [x] **AWS Backup decommissioned** (2026-05-13) — custom solution is now the
  sole backup system for all NSHM production data

---

## Configuration

### Example Configuration File (backup-config.yaml)

```yaml
# NSHM Backup Configuration

general:
  region: ap-southeast-2
  environment: production
  tags:
    Project: NSHM
    ManagedBy: backup-cli
    CostCenter: GNS-Science

sources:
  toshi:
    s3_buckets:
      - nzshm-toshi-api-data
    dynamodb_tables:
      - ToshiAPI-FileTable
      - ToshiAPI-ThingTable
    schedule:
      frequency: weekly
      day: sunday
      time: "02:00"
      timezone: Pacific/Auckland
    active_experiment_mode:
      enabled: false
      frequency: daily
      start_date: null
      end_date: null

  ths:
    s3_buckets:
      - nzshm-ths-dataset-prod
    schedule:
      frequency: weekly
      day: sunday
      time: "03:00"
      timezone: Pacific/Auckland

retention:
  hot_days: 30              # S3 Standard → Glacier Instant Retrieval at this age; kept forever (ADR-006)

restore:
  default_destination_type: temporary
  temporary_retention_days: 7
  dynamodb_always_new_table: true
  auto_approve_threshold: 100    # NZD
  dual_approval_threshold: 500   # NZD

notifications:
  ses:
    enabled: true
    source_email: noreply-backup@gns-nsdm.org.nz
    recipients:
      - admin@gns-nsdm.org.nz
      - ops-team@gns-nsdm.org.nz
  slack:
    enabled: true
    webhook_url_secret: backup-slack-webhook  # AWS Secrets Manager
    channel: "#nsdm-backups"
    notify_on:
      - backup_success
      - backup_failure
      - restore_initiated
      - restore_completed
      - test_failure

cost_tracking:
  enabled: true
  budget_alerts: true
  monthly_budget: 700  # NZD
  export_to_s3: s3://gns-finance-reports/nsdm-backup/

testing:
  weekly_small_test:
    enabled: true
    day: wednesday
    time: "10:00"
    sample_size_mb: 100
  monthly_table_restore:
    enabled: true
    day: first-monday
    time: "09:00"
    table: ToshiAPI-FileTable
  quarterly_full_drill:
    enabled: true
    months: [january, april, july, october]
    day: 15
    isolated_environment: true
```

---

## Security & Compliance

### IAM Strategy

- **Least privilege:** Each function has minimal required permissions
- **Role assumption:** Cross-account restore via IAM roles
- **Secrets management:** Slack webhooks, credentials in AWS Secrets Manager
- **Encryption:** All backups encrypted with KMS (AWS managed keys)

### Audit Trail

- CloudWatch Logs for all operations (retention: 1 year)
- S3 access logging for backup buckets
- Cost allocation tags for chargeback
- Monthly compliance reports

### Disaster Recovery

- Single-region initially (ap-southeast-2)
- Future: Cross-region replication of Glacier IR backups if compliance requires
- Runbook for full environment recovery
- Quarterly restore drills validate DR capability

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Data loss during transition | High | Low | Parallel run 2-3 months, restore drills before cutover |
| Glacier retrieval costs surprise | Medium | Medium | Dry-run previews, approval workflows, cost alerts |
| Lambda timeout for large exports | Medium | Low | Step Functions orchestration if needed, timeout monitoring |
| Configuration drift | Low | Medium | Infrastructure as Code, version-controlled config |
| Restore failures undetected | High | Low | Automated testing, notifications on all test results |
| Cost overrun | Medium | Low | Budget alerts, monthly reports, 15% buffer in estimates |

---

## Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Annual cost | < $10,000 NZD | AWS Cost Explorer |
| Backup success rate | > 99% | CloudWatch metrics |
| Restore success rate | 100% | Quarterly drills |
| Data loss incidents | 0 | Incident tracking |
| Time to restore (Standard) | < 1 hour | Test measurements |
| Time to restore (Glacier IR) | bound by copy throughput; no thaw step | Test measurements |

---

## Next Steps

### Before Implementation Starts

1. ✅ Confirm data volumes (completed: 9 TB S3, 18.3 GB DynamoDB)
2. ✅ Confirm retention policy (single Standard → Glacier IR transition at day 30, no expiry — see ADR-006)
3. ✅ Confirm single-region start (completed: yes)
4. ✅ Identify S3 bucket names and DynamoDB table names (alias system implemented)
5. ✅ Decide on Infrastructure as Code approach (Serverless Framework)
6. ⏳ Confirm Slack workspace and channel for alerts
7. ⏳ Identify initial admin email recipients for SES notifications
8. ✅ Approve this design plan

### Completed Tasks

**Phase 1 Step 1 (Week 1):**
1. ✅ Set up project structure with Typer CLI skeleton
2. ✅ Created AWS Lambda deployment package structure
3. ✅ Configured IAM roles and policies (no delete permission)
4. ✅ Implemented basic command structure (all subcommands)
5. ✅ Set up test infrastructure with pytest + moto

**Phase 1 Step 2 (Week 2):**
1. ✅ Configuration system with Pydantic models
2. ✅ YAML config loader with validation
3. ✅ S3 backup module with incremental sync
4. ✅ CloudWatch logging setup (JSON + text formats)
5. ✅ Serverless Framework configuration (serverless.yml)
6. ✅ Lambda handler with BackupTask schema
7. ✅ Test suite: 35 tests, 71% coverage

**Phase 2 (Week 3):**
1. ✅ DynamoDB PITR export to S3
2. ✅ EventBridge scheduling (add/remove/enable/disable, weekly/daily/hourly/minutely)
3. ✅ Lambda deployed and verified in sandbox (Serverless Framework v4, Docker pip, SSO credentials)
4. ✅ S3 Batch Operations for large buckets (s3_batch.py, create-backup-roles.py, 13 tests)
5. ✅ Manual vs scheduled backup conflict resolved (ManagedBy tag)
6. ✅ Sandbox demo tooling and guide
7. ✅ Test suite: 69 tests passing

**Phase 4 + 5 (Restore + Testing):**
1. ✅ S3 restore — direct copy with ETag-based skip (`s3_restore.py`)
2. ✅ DynamoDB PITR restore — async submit-and-return (`dynamodb_restore.py`)
3. ✅ `backup restore run` + `backup restore status` CLI commands
4. ✅ `backup test restore` — S3 sample restore + DynamoDB round-trip
5. ✅ `backup test integrity` — S3 ETag/count + DynamoDB export verification
6. ✅ S3 versioning on backup buckets (`version_retention_days` config field)
7. ✅ Last-run state persisted to `_state/last-run.json` in backup bucket
8. ✅ `pitr-watcher` Lambda — automatic PITR re-enable via SSM pending list + EventBridge
9. ✅ Split IAM roles: `nzshm-backup-reader` (read-only) + `nzshm-backup-restore` (restore ops)
10. ✅ `scripts/create-source-roles.py` — creates both roles, writes ARNs back to config
11. ✅ Verified end-to-end cross-account: Arkivalist backup + restore + PITR re-enable
12. ✅ Test suite: 152 tests passing

---

## Appendix A: Glossary

| Term | Definition |
|------|------------|
| PIT | Point-in-Time (DynamoDB consistent snapshot) |
| Glacier | AWS long-term archival storage service |
| Lifecycle Policy | Automated S3 object transition rules |
| EventBridge | AWS event scheduling service |
| SES | AWS Simple Email Service |
| RCU | Read Capacity Unit (DynamoDB) |
| DR | Disaster Recovery |

---

**Document Version:** 1.7
**Created:** 2026-03-09  **Last updated:** 2026-05-14
**Status:** AWS Backup decommissioned 2026-05-13. Custom solution is sole backup system. All 4 sources backed up daily (50.8M objects, 11 TB), restore-verified. Remaining: Phase 3 (notifications), full DR drill, cost verification.
**Owner:** NSHM DevOps Team
