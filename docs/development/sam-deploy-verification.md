# SAM deploy verification

A step-by-step runbook for verifying that `sam.yaml` (the SAM template)
produces a CloudFormation stack equivalent to the legacy
`serverless.yml` deploy. Run before merging
[PR #51](https://github.com/GNS-Science/nzshm-backup/pull/51), and
again any time the template changes materially.

Two-track approach:

1. **Local validation** — cheap, ~30 s, no AWS calls. Catches
   SAM-transform errors and dep-packaging issues.
2. **Side-stack deploy** — deploys SAM to a parallel CloudFormation
   stack so it can be compared against the existing sls stack
   without touching production.

Cleanup at the end removes the side stack so this leaves no
ongoing cost or operational footprint.

## Prerequisites

```bash
# SAM CLI is a dev dependency — pulled in by `uv sync --all-extras`
# (or just by `make sync`). No separate install step needed.
uv sync --all-extras
uv run sam --version   # should print "SAM CLI, version 1.x"

# Docker — required by `sam build --use-container` which mirrors
# the dockerizePip behaviour serverless-python-requirements gave us
# previously. Start Docker Desktop (or your local docker daemon).
docker info >/dev/null   # should succeed
```

Active AWS credentials (the same SSO export pattern the rest of
the project uses):

```bash
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"
aws sts get-caller-identity   # confirm the account ID matches the
                              # target deploy account
```

## Track 1 — local validation

### 1a. `sam validate`

Catches schema errors in the template and SAM-transform issues
without contacting AWS for anything but credential resolution:

```bash
sam validate --lint
```

Expected: `… is a valid SAM Template`. Any warnings from `--lint`
(cfn-lint backend) should be reviewed but most are advisory.

### 1b. `sam build --use-container`

Stages the package into `.aws-sam/build/` with Python deps installed
via Docker (matching the existing sls dockerizePip behaviour):

```bash
sam build --use-container
```

Expected duration ~2-4 min on first run (Docker image pull). Output
ends with `Build Succeeded`.

### 1c. Inspect the build output

```bash
ls -la .aws-sam/build/
# Should show three function build directories:
#   AlarmBridgeFunction/
#   BackupFunction/
#   PitrWatcherFunction/
```

Spot-check that the `nzshm_backup/` package and its deps landed in
one of them:

```bash
ls .aws-sam/build/BackupFunction/nzshm_backup/   # expect: lambda_handler.py, backup_engine.py, ...
ls .aws-sam/build/BackupFunction/boto3/          # expect: boto3 package — confirms deps were bundled
```

## Track 2 — side-stack deploy

> Deploys SAM to a parallel CloudFormation stack so the legacy sls
> stack (`nzshm-backup-service-prod`) keeps running unchanged.
> Nothing about production traffic changes during this verification.

### 2a. Configure `samconfig.toml`

First-time setup:

```bash
cp samconfig.example.toml samconfig.toml
```

Edit `samconfig.toml`:

- `stack_name` — set to `nzshm-backup-sam-test` (or any name distinct
  from the existing sls stack).
- `region` — your deploy region.
- `parameter_overrides` — set `Stage=test` so the SAM resources don't
  collide with the live `prod` ones (alarm names, topic names, etc.).
- `confirm_changeset = true` for the first deploy.

### 2b. Deploy

```bash
sam deploy
```

Expected: SAM uploads the built artefact to its managed S3 bucket,
creates a CloudFormation changeset, prints the resource summary,
and waits for confirmation. Review the changeset for the expected
12 resources, then confirm.

Deploy duration ~2-3 min for first-create.

### 2c. Verify the stack landed cleanly

```bash
aws cloudformation describe-stack-resources \
  --stack-name nzshm-backup-sam-test \
  --query 'StackResources[].[LogicalResourceId,ResourceType,ResourceStatus]' \
  --output table
```

Expect all 12 resources in `CREATE_COMPLETE`. Reference list:

| Logical ID | Type |
|---|---|
| `BackupLambdaRole` | `AWS::IAM::Role` |
| `BackupFunction` | `AWS::Lambda::Function` |
| `PitrWatcherFunction` | `AWS::Lambda::Function` |
| `AlarmBridgeFunction` | `AWS::Lambda::Function` |
| `BackupAlertsTopic` | `AWS::SNS::Topic` |
| `BackupReportsTopic` | `AWS::SNS::Topic` |
| `BackupLambdaErrorAlarm` | `AWS::CloudWatch::Alarm` |
| `BackupLambdaLogErrorFilter` | `AWS::Logs::MetricFilter` |
| `BackupLambdaLogErrorAlarm` | `AWS::CloudWatch::Alarm` |
| `PitrWatcherLambdaErrorAlarm` | `AWS::CloudWatch::Alarm` |
| `AlarmBridgeSnsSubscription` | `AWS::SNS::Subscription` |
| `AlarmBridgeInvokePermission` | `AWS::Lambda::Permission` |

### 2d. Smoke-test the deployed functions

Backup Lambda — async invoke, then check logs:

```bash
aws lambda invoke --region <region> \
  --function-name nzshm-backup-service-test-backup \
  --invocation-type Event \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"<a-source-alias>","trigger_type":"manual","dry_run":true}' \
  /tmp/sam-invoke.json
```

Then tail the log group:

```bash
aws logs tail /aws/lambda/nzshm-backup-service-test-backup \
  --since 5m \
  --follow
```

Expected: log lines showing the dry-run resolved successfully (no
exceptions, no `[ERROR]`). Stop the tail once the run completes.

Alarm-bridge Lambda — publish a synthetic alarm payload to the
alerts topic and confirm Slack receives it:

```bash
aws sns publish --region <region> \
  --topic-arn $(aws cloudformation describe-stacks \
                  --stack-name nzshm-backup-sam-test \
                  --query 'Stacks[0].Outputs[?OutputKey==`BackupAlertsTopicArn`].OutputValue' \
                  --output text) \
  --subject "ALARM: synthetic-test" \
  --message '{"AlarmName":"sam-verification-synthetic","NewStateValue":"ALARM","NewStateReason":"SAM deploy verification","Region":"<region>","StateChangeTime":"2026-06-23T00:00:00Z"}'
```

Expected: a Block Kit message appears in the Slack channel within
~10 s. If it doesn't, check the alarm-bridge function logs.

### 2e. Parity diff vs the legacy sls stack

Side-by-side IAM policy compare — the SAM role should grant the
same statements as the sls role:

```bash
aws iam get-role-policy \
  --role-name nzshm-backup-service-prod-ap-southeast-2-lambdaRole \
  --policy-name nzshm-backup-service-prod-lambda \
  --query PolicyDocument > /tmp/sls-policy.json

# SAM role name is generated by CFN; look it up:
SAM_ROLE=$(aws cloudformation describe-stack-resource \
            --stack-name nzshm-backup-sam-test \
            --logical-resource-id BackupLambdaRole \
            --query 'StackResourceDetail.PhysicalResourceId' \
            --output text)

aws iam list-role-policies --role-name $SAM_ROLE  # find the inline policy name, then:
aws iam get-role-policy \
  --role-name $SAM_ROLE \
  --policy-name <inline-policy-name> \
  --query PolicyDocument > /tmp/sam-policy.json

diff <(jq -S . /tmp/sls-policy.json) <(jq -S . /tmp/sam-policy.json)
```

Expected: empty diff, or only cosmetic ordering differences. Any
substantive Action / Resource / Effect difference is a bug to fix
in the template before this PR can merge.

Environment-variable compare on each function:

```bash
for fn in backup pitr-watcher alarm-bridge; do
  echo "=== $fn ==="
  diff <(aws lambda get-function-configuration \
          --function-name nzshm-backup-service-prod-$fn \
          --query 'Environment.Variables' --output json | jq -S .) \
       <(aws lambda get-function-configuration \
          --function-name nzshm-backup-service-test-$fn \
          --query 'Environment.Variables' --output json | jq -S .)
done
```

Account for expected differences: `NZSHM_STAGE` differs (`prod`
vs `test`), `BACKUP_REPORTS_TOPIC_ARN` differs (different stack →
different topic ARN). Anything else differing is suspect.

## Cleanup

```bash
sam delete --stack-name nzshm-backup-sam-test
```

CloudFormation will delete all 12 resources. Confirms reverse-
correctness too: SAM can both create and cleanly destroy the
stack.

## Cutover criteria

Before PR #51 was marked ready for review (all ticked during
Activity A side-stack verification, 2026-06-23):

- [x] Track 1 (1a, 1b, 1c) passes locally.
- [x] Track 2 (2a-2e) completes against a real AWS account, with
      all 12 resources `CREATE_COMPLETE` and parity diffs clean.
- [x] Smoke test on the deployed stack: backup Lambda dry-run
      completes without `[ERROR]` logs, synthetic alarm appears in
      Slack.

Before `serverless.yml` could be removed (this follow-up PR — all
ticked, hence this PR exists):

- [x] A real production deploy uses SAM successfully — Activity B
      cutover landed 2026-06-24 16:25 NZST. The sls stack was
      removed; the SAM stack of the same name took over with 15
      resources `CREATE_COMPLETE`.
- [x] At least one full daily backup cycle runs cleanly on the
      SAM-deployed Lambda. Two clean cycles to date: 2026-06-25
      and 2026-06-26 mornings — all 5 schedules fired, all three
      alarms remained OK, daily health report = GREEN 4/4.
- [x] PROD-DEPLOY-LOG records the cutover. Step 23 entry in
      `nzshm-backup-ops/docs/PROD-DEPLOY-LOG.md`.

This is the cutover safety rule from PR #49 / migration-doc §3a
applied to this specific migration step. The rule is now
satisfied, which is why this PR (removing `serverless.yml`) can
land.
