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
Rule Name                                     State      Schedule                         Local time
----------------------------------------------------------------------------------------------------
nzshm-backup-arkivalist-hourly               ENABLED    cron(0 * * * ? *)                → :00 past each hour (NZDT)
nzshm-backup-arkivalist-weekly               ENABLED    cron(0 14 ? * SAT *)             → Sunday 03:00 NZDT locally
```

The **Local time** column shows when the rule fires in the local timezone.

## Add a schedule

```bash
# Using plain UTC (HH:MM)
backup schedule add --source toshi --frequency daily --time 02:00

# Using a localised time — converted to UTC automatically
backup schedule add --source toshi --frequency daily  --time '02:00 NZST'
backup schedule add --source toshi --frequency hourly --time '12:30 NZDT'  # :30 past each hour NZDT

# Weekly with a full datetime — determines the UTC day-of-week
# e.g. Sunday 12:15 NZDT = Saturday 23:15 UTC → cron fires on SAT
backup schedule add --source toshi --frequency weekly --time '2026-03-29 12:15 NZDT'

# ISO 8601 is also accepted
backup schedule add --source toshi --frequency daily --time 2026-03-29T02:00:00+13:00
```

`--time` accepts:
- `HH:MM` — treated as UTC
- `HH:MM TZ` — e.g. `12:15 NZDT`; converted to UTC (NZST = UTC+12, NZDT = UTC+13, AEST = UTC+10, AEDT = UTC+11)
- `YYYY-MM-DD HH:MM TZ` — full datetime; for weekly schedules the UTC date determines the day-of-week
- ISO 8601 datetime

For `hourly`, only the minute component is used. For `minutely`, `--time` is ignored.

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

| Frequency | `--time` example | EventBridge expression | Notes |
|-----------|-----------------|----------------------|-------|
| `weekly` | `'2026-03-29 12:15 NZDT'` | `cron(15 23 ? * SAT *)` | UTC day may differ from local day |
| `daily` | `'02:00 NZST'` | `cron(0 14 * * ? *)` | |
| `hourly` | `'12:30 NZDT'` | `cron(30 * * * ? *)` | Only minute used |
| `minutely` | (ignored) | `rate(1 minute)` | Testing only |

`minutely` is for testing only — it fires every minute regardless of `--time`.

## Lambda ARN prerequisite

Schedules without a `lambda_arn` create the EventBridge rule but cannot register
the target. After deploying the Lambda:

1. Copy the function ARN from the `sls deploy` output
2. Add it to `backup-config.yaml`:
   ```yaml
   general:
     lambda_arn: arn:aws:lambda:ap-southeast-2:595842668254:function:nzshm-backup-prod-backup
   ```
3. Re-run `backup schedule add` — it will register the target against the existing rule

See [Lambda Deployment](../development/lambda-deployment.md) for full deploy instructions.
