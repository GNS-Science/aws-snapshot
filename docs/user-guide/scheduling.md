# Scheduling

Backups run automatically via AWS EventBridge rules targeting the backup Lambda.
Each rule is named `nzshm-backup-{source}-{frequency}`.

## View current schedules

```bash
backup schedule show
backup schedule show --output json
```

Output:

```
Rule Name                                     State      Schedule
--------------------------------------------------------------------------------
nzshm-backup-toshi-weekly                    ENABLED    cron(0 14 ? * SUN *)
nzshm-backup-ths-weekly                      ENABLED    cron(30 14 ? * SUN *)
```

## Add a schedule

```bash
# Weekly backup at 14:00 UTC on Sundays
backup schedule add --source toshi --frequency weekly --time 14:00

# Daily backup at 02:00 UTC (Active Experiment Mode)
backup schedule add --source toshi --frequency daily --time 02:00

# Hourly backup at :30 past each hour
backup schedule add --source toshi --frequency hourly --time 00:30
```

Times must be in **UTC**. NZST = UTC+12, NZDT = UTC+13.

The command creates (or updates) the EventBridge rule and registers the Lambda
as the target. If `general.lambda_arn` is not set in the config, the rule is
created but a warning is printed — wire up the Lambda ARN after deployment.

## Remove a schedule

```bash
backup schedule remove --source toshi --frequency weekly
```

Removes the EventBridge rule and deregisters the Lambda target.

## Enable / disable without removing

```bash
# Disable all rules for a source (e.g. during maintenance)
backup schedule disable --source toshi

# Re-enable
backup schedule enable --source toshi

# Enable/disable a specific frequency only
backup schedule disable --source toshi --frequency daily
backup schedule enable --source toshi --frequency daily
```

## Active Experiment Mode

During periods of high data churn (sensitivity analyses), temporarily switch to
daily exports:

```bash
# Enable daily backups
backup schedule add --source toshi --frequency daily --time 02:00

# When experiments finish, remove daily and keep weekly
backup schedule remove --source toshi --frequency daily
```

## Cron expressions generated

| Frequency | `--time` | EventBridge expression |
|-----------|----------|----------------------|
| `weekly` | `14:00` | `cron(0 14 ? * SUN *)` |
| `daily` | `02:00` | `cron(0 2 * * ? *)` |
| `hourly` | `00:30` | `cron(30 * * * ? *)` |
| `minutely` | (ignored) | `rate(1 minute)` |

`minutely` is for testing only — it fires every minute regardless of `--time`.

## Lambda ARN prerequisite

Schedules without a `lambda_arn` create the EventBridge rule but cannot register
the target. After deploying the Lambda:

1. Copy the function ARN from the `sls deploy` output
2. Add it to `backup-config.yaml`:
   ```yaml
   general:
     lambda_arn: arn:aws:lambda:ap-southeast-2:345678901234:function:nzshm-backup-prod-backup
   ```
3. Re-run `backup schedule add` — it will register the target against the existing rule

See [Lambda Deployment](../development/lambda-deployment.md) for full deploy instructions.
