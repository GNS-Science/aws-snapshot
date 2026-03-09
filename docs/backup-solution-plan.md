# NSHM Backup Solution - Design Plan

## Executive Summary

**Goal:** Replace expensive AWS Backup solution ($1,700 NZD/month = $20,400 NZD/year) with custom AWS-native solution reducing costs by 64% while maintaining coverage for human error and infrastructure risks.

**Target Architecture:** Serverless Python/Click CLI running on AWS Lambda with S3 Glacier integration.

**Projected Savings:** $13,080 NZD/year (from $20,400 to $7,320)

---

## Data Sources

| Source | Size | Storage Type | Backup Method |
|--------|------|--------------|---------------|
| ToshiAPI - FileTable | 2.3 GB | DynamoDB | Point-in-Time Export to S3 |
| ToshiAPI - ThingTable | 16 GB | DynamoDB | Point-in-Time Export to S3 |
| ToshiBucket | 8 TB | S3 | S3 Copy + Lifecycle Policies |
| THS_dataset_prod | 1 TB | S3 | S3 Copy + Lifecycle Policies |
| **Total** | **9 TB + 18.3 GB** | | |

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
| Warm | 31-90 days | S3 Glacier Instant | $0.007 | Milliseconds |
| Cold | 91-365 days | S3 Glacier Deep Archive | $0.0017 | 12-48 hours |
| Expire | 365+ days | Delete | - | - |

**Note:** Starting with single-region (ap-southeast-2 Sydney). Cross-region can be added later for Deep Archive tier only if compliance requires.

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

### Current State (AWS Backup)
- Monthly: $1,700 NZD
- Annual: $20,400 NZD

### Proposed Solution (30-Day Hot Policy)

```
Steady State Monthly Costs:
├─ S3 Standard (0-30 days):     9 TB × 1 × $0.036 = $324 NZD
├─ S3 Glacier Instant (31-90):  9 TB × 2 × $0.007 = $126 NZD
├─ S3 Deep Archive (91-365):    9 TB × 9 × $0.0017 = $138 NZD
├─ DynamoDB Exports:            18.3 GB × 4 × $0.16 = $12 NZD
├─ Lambda + SES:                ~$10 NZD
├─ Testing overhead:            ~$8 NZD
└─ TOTAL:                       ~$618 NZD/month = $7,420 NZD/year
```

### Comparison Summary

| Solution | Monthly (NZD) | Annual (NZD) | Savings/Year | Savings % |
|----------|---------------|--------------|--------------|-----------|
| Current (AWS Backup) | $1,700 | $20,400 | - | - |
| Proposed (30-day hot) | $618 | $7,420 | $12,980 | 64% |
| Proposed (15-day hot) | $488 | $5,860 | $14,540 | 71% |

---

## CLI Tool Design

### Command Structure

```bash
# Schedule management
$ backup schedule show          # View current schedule
$ backup schedule set --frequency weekly --mode standard
$ backup schedule enable/disable

# Manual backup trigger
$ backup run --source toshi     # Manual trigger
$ backup run --source ths
$ backup run --all

# Restore operations
$ backup restore list --source toshi --limit 10
$ backup restore preview --date 2026-02-15 --source toshi --target-bucket test-restore
$ backup restore run --date 2026-02-15 --source toshi --target-bucket <dest>
$ backup restore run --date 2026-02-15 --table ThingTable --target-account <aws-id>
$ backup restore run --date 2026-02-15 --prefix "models/2026/" --target-bucket <dest>
$ backup restore cancel --job-id <id>

# Testing & validation
$ backup test restore --latest --validate-integrity --report-only
$ backup test integrity --date 2026-02-15 --detail
$ backup test full-drill --source toshi --isolated-environment

# Status & reporting
$ backup status                 # Current state, last run, next run
$ backup report --period 30d    # Cost & activity report
$ backup report compliance --format pdf

# Cost management
$ backup costs predict          # Before/after cost projection
$ backup costs report --last-month
$ backup costs breakdown --by-source
$ backup costs export --format csv

# Configuration
$ backup config show
$ backup config set --key retention.days --value 365
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
| S3 Glacier Instant (31-90 days) | Milliseconds | $0.03/GB | Recent historical restores |
| S3 Glacier Deep Archive (91-365 days) | 12-48 hours | $0.05/GB + expedite | Compliance, disasters |

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
- [x] Lifecycle policy attachment (30/90/365 day tiers)
- [x] Globally unique backup bucket naming: `{bucket}-backup-{region}-{account_id}`
- [x] Delete protection via IAM (no s3:DeleteObject permission)
- [x] CloudWatch-compatible logging (JSON format option)
- [x] Lambda handler with Pydantic task schema
- [x] Serverless Framework config (serverless.yml)
- [x] Test suite: 35 tests, 71% coverage
- [x] All lint checks passing (ruff + black)

### Phase 2: DynamoDB + Scheduling (Week 3)
- [ ] DynamoDB export integration (PIT export to S3)
- [ ] EventBridge scheduling rules
- [ ] Lambda function deployment
- [ ] IAM roles and policies

### Phase 3: Notifications + Reporting (Week 4)
- [ ] SES email integration
- [ ] Slack webhook integration
- [ ] Cost tracking implementation
- [ ] Status/reporting commands
- [ ] Budget alerts setup

### Phase 4: Restore Functionality (Week 5-6)
- [ ] Restore list/preview commands
- [ ] S3 restore operations (all tiers)
- [ ] DynamoDB import operations
- [ ] Dry-run/cost estimation
- [ ] Glacier retrieval handling
- [ ] Approval workflow implementation

### Phase 5: Testing & Validation (Week 7)
- [ ] Automated restore tests
- [ ] Integrity validation (checksums, counts)
- [ ] Test scheduling (EventBridge)
- [ ] Compliance reporting (HTML/PDF)
- [ ] Test result notifications

### Phase 6: Parallel Run + Cutover (Week 8-10)
- [ ] Parallel run with AWS Backup (2-3 months)
- [ ] Restore drill validation
- [ ] Cost verification
- [ ] Documentation
- [ ] Cutover planning
- [ ] AWS Backup decommission

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
  hot_days: 30              # S3 Standard
  warm_days: 90             # S3 Glacier Instant
  cold_days: 365            # S3 Glacier Deep Archive
  max_age_days: 365         # Delete after this

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
- Future: Cross-region replication for Deep Archive tier only
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
| Time to restore (Deep Archive) | < 48 hours | Test measurements |

---

## Next Steps

### Before Implementation Starts

1. ✅ Confirm data volumes (completed: 9 TB S3, 18.3 GB DynamoDB)
2. ✅ Confirm retention policy (30/90/365 days)
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

---

## Appendix A: AWS Pricing Reference (ap-southeast-2)

| Service | Unit | USD | NZD (approx) |
|---------|------|-----|--------------|
| S3 Standard | GB-month | $0.023 | $0.036 |
| S3 Glacier Instant | GB-month | $0.004 | $0.007 |
| S3 Glacier Deep Archive | GB-month | $0.00099 | $0.0017 |
| DynamoDB Export | GB | $0.10 | $0.16 |
| Glacier Retrieval (Instant) | GB | $0.03 | $0.05 |
| Glacier Retrieval (Deep Archive) | GB | $0.05 | $0.08 |
| AWS Lambda | 1M requests | $0.20 | $0.32 |
| AWS Lambda | GB-second | $0.0000166667 | $0.000027 |
| SES | 1000 emails | $0.10 | $0.16 |

*Exchange rate: 1 USD = 1.57 NZD (approximate, Feb 2026)*

---

## Appendix B: Glossary

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

**Document Version:** 1.0  
**Created:** 2026-03-09  
**Status:** Approved for Implementation  
**Owner:** NSHM DevOps Team  
