# Cost Management

## Cost overview

The custom backup solution replaces AWS Backup (~$1,700 NZD/month) with an
S3 lifecycle + DynamoDB PITR approach. Production steady-state cost (all four
sources aged into Glacier Instant Retrieval, per
[ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md))
is ~$1,300 NZD/year (~$108/month).

For full pricing tables, tier breakdown, and AWS Backup comparison see:
[Cost Model](../architecture/cost-model.md)

## Costs command

The `backup costs` subcommand is planned for Phase 3 (not yet implemented):

```bash
# Planned — not yet available
backup costs report --last-month
backup costs breakdown --by-source
backup costs predict --current 20400 --target 7420
```

Until then, use the AWS Cost Explorer with the `ManagedBy: backup-cli` tag filter
to track costs by source.

## Cost drivers

| Driver | Cost | Notes |
|--------|------|-------|
| S3 storage (Glacier IR, aged) | ~$0.007/GB/month | Bulk of the 11.7 TB corpus after the first 30 days |
| S3 storage (Standard) | ~$0.036/GB/month | First 30 days after each object is (re)written |
| DynamoDB PITR | Free | Included in table pricing |
| DynamoDB exports | ~$3/run | 18.3 GB × $0.16/GB |
| Lambda + EventBridge | ~$10/month | Fixed overhead |
| Glacier IR retrieval (DR only) | ~$0.079/GB | One-time, emergency only |

## Managing costs during Active Experiment Mode

Production runs DynamoDB exports weekly alongside S3 (same schedule). During periods of
active sensitivity analysis with high data churn, consider switching to daily exports
and monitoring S3 costs closely:

```bash
# Switch to daily DynamoDB exports during active experiments
backup schedule add --source toshi --frequency daily --time "20:15 NZST"

# Switch back to weekly cadence when experiments complete
backup schedule remove --source toshi --frequency daily
```

See [Retention & Costs](../design/retention-strategy-and-costs.md#active-experiment-mode)
for the cost impact of different export frequencies.

## Budget alerts

Configure a monthly budget threshold in `backup-config.yaml`:

```yaml
cost_tracking:
  enabled: true
  budget_alerts: true
  monthly_budget: 700  # NZD — alert if costs exceed this
```

This creates an AWS Budgets alert. The 700 NZD default is intentionally
conservative — steady-state costs should be ~$36/month. The high threshold
provides headroom during initial sync (first 3 months while 11.7 TB ages
through Standard and Glacier Instant tiers).
