# Cost Management

## Cost overview

The custom backup solution replaces AWS Backup (~$1,700 NZD/month) with an
S3 lifecycle + DynamoDB PITR approach that runs at ~$29 NZD/month at steady state.

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
| S3 storage (Deep Archive) | ~$0.0017/GB/month | Bulk of the 9 TB corpus after 3 months |
| DynamoDB PITR | Free | Included in table pricing |
| DynamoDB exports | ~$3/run | 18.3 GB × $0.16/GB |
| Lambda + EventBridge | ~$10/month | Fixed overhead |
| Glacier retrieval (DR only) | ~$0.126/GB | One-time, emergency only |

## Managing costs during Active Experiment Mode

When scientists run sensitivity analyses with high data churn, switch to more
frequent DynamoDB exports and monitor S3 costs:

```bash
# Increase to weekly DynamoDB exports during active experiments
backup schedule add --source toshi --frequency weekly --time 14:00

# Switch back to monthly cadence when experiments complete
backup schedule remove --source toshi --frequency weekly
backup schedule add --source toshi --frequency monthly --time 14:00
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
conservative — steady-state costs should be ~$29/month. The high threshold
provides headroom during initial sync (~$588/month for first 3 months).
