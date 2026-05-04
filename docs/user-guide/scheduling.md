# Scheduling

Backups run automatically via AWS EventBridge rules. Rules target the backup
Lambda by default. CodeBuild targeting is available as a fallback for sources
that exceed Lambda's 15-minute timeout, but since the switch to inventory-based
Athena manifest generation (`batch_manifest_mode: inventory`) all production
sources run on Lambda. Each rule is named `nzshm-backup-{source}-{frequency}`.

## View current schedules

```bash
backup schedule show
backup --output json schedule show
```

Output:

```
Rule Name                                     State      Schedule               Target     Target detail                        Local time
----------------------------------------------------------------------------------------------------------------------------------------------------
nzshm-backup-arkivalist-hourly               ENABLED    cron(0 * * * ? *)      lambda     nzshm-backup-service-prod-backup      → :00 past each hour (NZDT)
nzshm-backup-ths-weekly                      ENABLED    cron(15 8 ? * WED *)    lambda     nzshm-backup-service-prod-backup     → Wednesday 20:15 NZST locally
```

The **Target** and **Target detail** columns show whether the rule triggers
Lambda or CodeBuild and which function/project is wired.

`backup --output json schedule show` includes per-rule target metadata:
- `target_type`: `lambda`, `codebuild`, `mixed`, or `none`
- `targets`: raw EventBridge target objects

## Check scheduler health for a source

```bash
backup schedule health --source ths --frequency weekly
backup --output json schedule health --source ths
```

`backup schedule health` combines three signals in one view:
- EventBridge rule state/schedule/target wiring
- EventBridge invocation + failed-invocation counts over a lookback window
- latest CodeBuild build status when the target type is `codebuild`

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

To target CodeBuild instead of Lambda:

```bash
backup schedule add \
  --source ths \
  --frequency weekly \
  --time '2026-04-22 20:15 NZST' \
  --target codebuild \
  --codebuild-project-arn arn:aws:codebuild:ap-southeast-2:737696831915:project/nzshm-backup-ths \
  --target-role-arn arn:aws:iam::737696831915:role/nzshm-backup-events-codebuild
```

`--target codebuild` requires both `--codebuild-project-arn` and
`--target-role-arn`.

## Remove a schedule

```bash
backup schedule remove --source toshi --frequency weekly
```

Removes the EventBridge rule and deregisters all targets (Lambda and/or CodeBuild).

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

## Mixed-target release checklist (Lambda + CodeBuild)

> **Note:** Since the switch to inventory-based Athena manifest generation
> (2026-05-04), all production sources run on Lambda. This checklist is retained
> for cases where CodeBuild targeting is needed as a fallback (e.g. sources that
> require long-running compute beyond Lambda's 15-minute limit).

When production uses a mix of Lambda-targeted and CodeBuild-targeted schedules,
use this checklist to avoid drift:

1. **Preflight**
   - Confirm target branch/commit SHA
   - Run `backup check` for impacted sources

2. **Config push**
   - Update `backup-config.production.yaml`
   - Push config: `BACKUP_CONFIG_PATH=backup-config.production.yaml uv run backup config push --stage prod`

3. **Lambda path (if any lambda targets are active)**
   - Deploy: `serverless deploy --stage prod`
   - Verify function config and IAM

4. **CodeBuild path (if any codebuild targets are active)**
   - Upload fresh source artifact zip
   - Update/create CodeBuild project to point at new artifact
   - Verify buildspec, compute size, timeout, roles, and log group

5. **Scheduler wiring**
   - `backup schedule show`
   - Confirm each source has exactly one target mode (no Lambda+CodeBuild double targets)

6. **Smoke evidence**
   - Trigger one manual run per changed source
   - Confirm start signal, batch job submission, and no immediate errors

7. **Rollback readiness**
   - Lambda rollback: redeploy previous artifact and restore lambda target
   - CodeBuild rollback: point project to previous source artifact and restore prior target mode

## CodeBuild fallback: run-now and progress monitoring

> **Legacy path.** Before inventory-based Athena manifests, THS used CodeBuild
> because inline manifest preparation exceeded Lambda's 15-minute timeout (~50-60
> minutes for 4M objects). With `batch_manifest_mode: inventory`, Athena generates
> the manifest in seconds, so all sources now run on Lambda. This section is
> retained for reference if CodeBuild targeting is needed in future.

### Trigger a CodeBuild run manually

```bash
AWS_PROFILE=nshm-backup-admin aws codebuild start-build \
  --region ap-southeast-2 \
  --project-name nzshm-backup-ths-backup
```

### Monitor CodeBuild progress

1) Poll build status:

```bash
AWS_PROFILE=nshm-backup-admin aws codebuild list-builds-for-project \
  --region ap-southeast-2 \
  --project-name nzshm-backup-ths-backup \
  --sort-order DESCENDING \
  --max-items 1

AWS_PROFILE=nshm-backup-admin aws codebuild batch-get-builds \
  --region ap-southeast-2 \
  --ids <BUILD_ID> \
  --query 'builds[0].{Status:buildStatus,Start:startTime,End:endTime,Log:logs.deepLink}'
```

2) Follow build logs:

```bash
AWS_PROFILE=nshm-backup-admin aws logs tail \
  "/aws/codebuild/nzshm-backup-ths-backup" \
  --region ap-southeast-2 \
  --follow
```

### Monitor S3 Batch progress after job submission

S3 Batch progress is only available after `CreateJob` succeeds. During manifest prep,
`backup status` may show no batch jobs yet.

Use `backup schedule health` to confirm scheduler/build-side progress while still
in pre-submit phases.

```bash
AWS_PROFILE=nshm-backup-admin \
BACKUP_CONFIG_PATH=backup-config.production.yaml \
uv run backup status --source ths --output json
```
