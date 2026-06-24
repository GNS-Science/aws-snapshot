# SAM cutover runbook

Step-by-step procedure for replacing the Serverless Framework prod
stack with the SAM-deployed stack. Run this *after* the
[SAM deploy verification runbook](sam-deploy-verification.md) has
passed against a side-stack and PR #51 has been merged to `main`.

Out-of-scope: the side-stack verification itself. That's a separate
document and should already be done before this runbook starts.

## What you're doing

Replacing the 20-resource sls CFN stack `nzshm-backup-service-prod`
with the 15-resource SAM CFN stack of the same name. Same Lambda
function names, same SNS topic names, same alarm semantics —
different CloudFormation lineage, different IAM role logical ID,
different EventBridge rule name for the pitr-watcher.

Because the logical IDs differ between the two stacks, CloudFormation
cannot update sls into SAM in place. The cutover is:

1. `sls remove --stage prod` (deletes the sls stack — Lambda goes
   away)
2. `sam deploy` with `Stage=prod` (creates the new SAM stack with
   same physical names)

In between, there is a **~3-5 minute window** with no backup Lambda
deployed. Scheduled backups firing in this window will fail; nothing
else breaks (source buckets, backup buckets, Athena DB, SSM config,
and `backup-config.production.yaml` are all outside the stack).

## Pre-cutover checklist

Tick all of these before starting. Stop if any one fails.

- [ ] **PR #51 is merged to `main`.** SAM template + Makefile are on
      the canonical branch you'll be deploying from.
- [ ] **PR #50 and PR #52 are merged** (docs + identifier scrubs).
      Not strictly required for the cutover itself, but operators
      reading docs during cutover want consistency.
- [ ] **The verification runbook has been run end-to-end against a
      side-stack within the last week.** Six template fixes were
      uncovered during the first verification; re-running confirms
      no new regressions.
- [ ] **`backup-config.production.yaml` is up to date.** This drives
      `backup notifications apply` post-cutover. If it's stale,
      subscribers will be wrong.
- [ ] **Local prerequisites**: `aws-sam-cli` installed, Docker
      running, AWS creds for the backup account active, `sls` (v4)
      installed for the removal step.
- [ ] **Maintenance window selected**: Mon-Wed, 10:30-11:00 NZST.
      No scheduled backup fires for ~22 h afterwards; avoids
      Friday/weekend recovery scenarios.
- [ ] **No DynamoDB restore in flight.** The pitr-watcher EventBridge
      rule will be disabled during cutover. If a restore is pending,
      either wait for it to complete or accept that PITR re-enable
      will be delayed.
- [ ] **Operator coordination**: brief other backup-account operators
      that the cutover is happening. Their dashboards will show the
      stack changing during the window.

## State capture (run before any destructive command)

Read-only. These outputs become the input to the post-cutover
restoration steps. Save them somewhere local — `/tmp/cutover-state/`
is fine.

```bash
mkdir -p /tmp/cutover-state
cd /tmp/cutover-state

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"

# Current per-source EventBridge schedules. These survive sls remove
# (they're CLI-created, not CFN-managed) but their target functions
# get deleted, so we'll recreate them to refresh the Lambda::Permission.
backup schedule show > schedules-before.txt

# Current alarm subscribers (both topics). These vanish with the sls
# stack and need to be reapplied from backup-config.production.yaml
# via `backup notifications apply`.
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:ap-southeast-2:<ACCOUNT_ID>:nzshm-backup-alerts-prod \
  --query 'Subscriptions[].[Protocol,Endpoint]' --output table \
  > alerts-subscribers-before.txt
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:ap-southeast-2:<ACCOUNT_ID>:nzshm-backup-reports-prod \
  --query 'Subscriptions[].[Protocol,Endpoint]' --output table \
  > reports-subscribers-before.txt

# Current sls stack resource list — proof-of-state for the
# PROD-DEPLOY-LOG entry.
aws cloudformation describe-stack-resources \
  --stack-name nzshm-backup-service-prod \
  --query 'StackResources[].[LogicalResourceId,ResourceType,PhysicalResourceId]' \
  --output table > sls-stack-before.txt

# Current alarm states. Useful for confirming no live alarms during
# cutover (we don't want to cut over while toshi is RED).
aws cloudwatch describe-alarms \
  --alarm-name-prefix nzshm-backup \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output table \
  > alarm-states-before.txt
```

Sanity-check `alarm-states-before.txt`. Any `ALARM` state on a
`nzshm-backup-lambda-*-errors-prod` alarm means the live sls stack is
unhealthy — fix that first, then come back to the cutover.

## Step 1 — Record intent in PROD-DEPLOY-LOG

Open `nzshm-backup-ops/docs/PROD-DEPLOY-LOG.md` and append a section:

```markdown
## Step <N>: SAM cutover from Serverless Framework (<YYYY-MM-DD>)

**Operator:** <name>
**Maintenance window:** <YYYY-MM-DD> <HH:MM>-<HH:MM> NZST
**Pre-state:** sls stack `nzshm-backup-service-prod` (20 resources);
state captured in `/tmp/cutover-state/sls-stack-before.txt`.
**Target state:** SAM stack `nzshm-backup-service-prod` (15
resources) per PR #51.
**Rollback path:** `sls deploy --stage prod` from the same operator
machine (`serverless.yml` retained on `main` through cutover).

### Procedure

(in progress — fill in as steps complete)
```

This entry stays open through the cutover and gets the
per-step results appended as you go. It's both the audit trail
and the recovery doc if anything goes sideways.

## Step 2 — Disable the pitr-watcher rule

```bash
aws events disable-rule --name nzshm-backup-pitr-watcher \
  --region ap-southeast-2
```

The rule is disabled by default (`enabled: false` in
`serverless.yml`) but a previous restore may have enabled it. This
ensures the watcher stops firing during the cutover window. The
new SAM-deployed rule will be `nzshm-backup-pitr-watcher-prod` (Stage-
suffixed), so the old rule will be deleted in step 3 and is not
needed afterwards.

## Step 3 — `sls remove --stage prod`

```bash
AWS_PROFILE=<aws-profile> npx sls remove --stage prod
```

CloudFormation deletes all 20 sls-stack resources. Duration ~2-3
min. Watch for `Stack delete complete` in the output.

If the delete stalls (rare — usually a stuck IAM role or a SNS
subscription with a dependent), inspect via:

```bash
aws cloudformation describe-stack-events \
  --stack-name nzshm-backup-service-prod \
  --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`]'
```

and resolve manually. Common stalls:

- **SNS subscription stuck pending confirmation** — usually delete
  the subscription via the SNS console.
- **IAM role still referenced** — usually a Lambda::Permission that
  needs explicit delete first.

**Sub-checkpoint:** confirm the stack is fully gone before
proceeding:

```bash
aws cloudformation describe-stacks --stack-name nzshm-backup-service-prod
# Expect: "Stack with id nzshm-backup-service-prod does not exist"
```

**The window of no-Lambda starts now.**

## Step 4 — `sam deploy` with `Stage=prod`

You need a `samconfig.toml` at the repo root pinning prod values.
The example in `samconfig.example.toml` is a starting point — for
the GNS prod cutover, the canonical version lives in
`nzshm-backup-ops/` (per the shim strategy in PR #49). Copy it
across or write it inline:

```toml
version = 0.1

[default]
[default.deploy.parameters]
stack_name = "nzshm-backup-service-prod"
region = "ap-southeast-2"
resolve_s3 = true
s3_prefix = "nzshm-backup-service-prod"
capabilities = "CAPABILITY_NAMED_IAM"
confirm_changeset = true
fail_on_empty_changeset = false
parameter_overrides = "Stage=prod ServiceName=nzshm-backup-service BatchRoleArn=arn:aws:iam::<SOURCE_ACCOUNT_ID>:role/nzshm-backup-batch-role SlackWebhookSecretName=backup-slack-webhook"

[default.build.parameters]
use_container = true
```

Deploy:

```bash
# .venv must be outside the source tree during SAM build — its
# broken symlinks-in-container will fail CopySource. See
# verification runbook §1.
mv .venv /tmp/saved-venv-cutover
make sam-build               # runs uv export + sam build --use-container
sam deploy                   # uses samconfig.toml; confirm_changeset=true
                             # gives a final go/no-go pause
mv /tmp/saved-venv-cutover .venv
```

The `confirm_changeset = true` is deliberate. SAM will print the
proposed changeset (expected 15 resources to CREATE) and pause for
"Y/n" before applying. **Read the resource list carefully.** Look
for:

- Expected: 3 functions, 1 IAM role, 1 log group, 2 SNS topics,
  3 alarms, 1 metric filter, 1 EventBridge rule, 2 Lambda
  permissions, 1 SNS subscription = 15 resources, all CREATE.
- Unexpected: any DELETE or REPLACE. There should be none — the
  sls stack is gone, so SAM starts from scratch.

Type `Y` to confirm. SAM deploys. Duration ~2-3 min. Watch for
`Successfully created/updated stack`.

**The window of no-Lambda ends here.**

**Sub-checkpoint:** verify all 15 resources are `CREATE_COMPLETE`:

```bash
aws cloudformation describe-stack-resources \
  --stack-name nzshm-backup-service-prod \
  --query 'StackResources[].[LogicalResourceId,ResourceStatus]' \
  --output table
```

## Step 5 — Re-attach SNS subscribers

The SNS topics (`nzshm-backup-alerts-prod`, `nzshm-backup-reports-prod`)
have the same physical names but are brand-new CFN resources — all
prior subscriptions are gone.

```bash
backup notifications apply
```

This reads `backup-config.production.yaml` and creates email
subscriptions on both topics. Confirm:

```bash
backup notifications show
diff <(aws sns list-subscriptions-by-topic \
        --topic-arn arn:aws:sns:ap-southeast-2:<ACCOUNT_ID>:nzshm-backup-alerts-prod \
        --query 'Subscriptions[].Endpoint' --output text | tr '\t' '\n' | sort) \
     <(grep email /tmp/cutover-state/alerts-subscribers-before.txt \
        | awk '{print $4}' | sort)
# Expect: empty diff (same subscribers as before)
```

The alarm-bridge Lambda subscription is recreated by SAM
automatically (it's `AlarmBridgeSnsSubscription` in the template).

## Step 6 — Recreate per-source schedules

Per-source schedules are CLI-created (not CFN-managed), so they
survived the sls remove — but their `lambda:InvokePermission`
allowing EventBridge to invoke the backup function is gone with
the old Lambda. Cleanest path: remove and re-add each schedule via
the CLI, which refreshes both rule and permission.

For each source in `schedules-before.txt`:

```bash
# e.g. if schedules-before.txt shows weka and toshi on daily schedules:
backup schedule remove --source weka --frequency daily
backup schedule add    --source weka --frequency daily --time "09:45 NZST"

backup schedule remove --source toshi --frequency daily
backup schedule add    --source toshi --frequency daily --time "09:45 NZST"

# repeat for each source previously scheduled.
backup schedule show > /tmp/cutover-state/schedules-after.txt
diff /tmp/cutover-state/schedules-before.txt /tmp/cutover-state/schedules-after.txt
# Expect: same schedule list as before (only rule ARNs may differ).
```

## Step 7 — Smoke test the deployed stack

A dry-run backup against one source. Same exercise as the
verification runbook's Track 2d-1, but now against the prod stack.

```bash
aws lambda invoke --region ap-southeast-2 \
  --function-name nzshm-backup-service-prod-backup \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"weka","trigger_type":"manual","dry_run":true}' \
  /tmp/cutover-smoke.json
cat /tmp/cutover-smoke.json
```

Expect a clean `statusCode 200` response (this run is against the
real prod config so unlike the side-stack it won't hit the
config-not-found path). Then check the log group has no `[ERROR]`
lines:

```bash
aws logs tail /aws/lambda/nzshm-backup-service-prod-backup \
  --region ap-southeast-2 --since 5m \
  | grep -E '\[ERROR\]|Traceback' || echo "(clean)"
```

If `(clean)`, the new stack handled an end-to-end dry-run
correctly. Synthetic Slack alarm test:

```bash
aws sns publish --region ap-southeast-2 \
  --topic-arn arn:aws:sns:ap-southeast-2:<ACCOUNT_ID>:nzshm-backup-alerts-prod \
  --subject "ALARM: cutover-smoke" \
  --message '{"AlarmName":"cutover-smoke","NewStateValue":"ALARM","NewStateReason":"SAM cutover post-deploy smoke","Region":"ap-southeast-2","StateChangeTime":"<NOW-ISO8601>"}'
```

Expect a message in Slack within ~10 s. (Real subscribers will also
see this email — clearly-labeled subject avoids confusion.)

## Step 8 — Manual `backup run` end-to-end

The first **real** (non-dry-run) backup the new stack handles.
Pick a source you're confident in:

```bash
backup run --source weka
```

Expect successful Athena UNLOAD, manifest, and S3 Batch job
submission (or `row_count=0` if there's no diff — that's fine
too). The full per-source pipeline gets exercised end-to-end on
real resources.

Then check the next day's daily health report (fires 09:45 NZST).
That's the first time the SAM-deployed stack handles a scheduled
fire end-to-end. If it lands GREEN, cutover is complete.

## Step 9 — Finalise the PROD-DEPLOY-LOG entry

Append to the entry opened in Step 1:

```markdown
### Procedure

| Step | Time | Result |
|---|---|---|
| 2 — Disable pitr-watcher rule | <HH:MM> | (already disabled / done) |
| 3 — sls remove | <HH:MM> | Stack deleted in ~Xs |
| 4 — sam deploy | <HH:MM> | 15 resources CREATE_COMPLETE in ~Xs |
| 5 — backup notifications apply | <HH:MM> | N subscribers reattached, diff clean |
| 6 — Recreate schedules | <HH:MM> | N schedules remove+add, diff clean |
| 7 — Smoke test (lambda invoke + SNS publish) | <HH:MM> | Both clean, Slack received |
| 8 — Manual backup run --source weka | <HH:MM> | row_count=X, S3 Batch job <id> |
| 9 — Daily health report (next morning) | <HH:MM> | GREEN |

### Post-state

- SAM stack `nzshm-backup-service-prod` deployed (commit <SHA>).
- `serverless.yml` retained on `main` as the rollback path until
  follow-up PR removes it.
- PR <N> opened to remove `serverless.yml` after 1 week of
  cleanly-running cycles.
```

## Rollback procedure

Only if Step 7 or Step 8 fails *and* roll-forward isn't feasible
within ~30 min. Acceptance criteria for rollback:

- Lambda init errors that aren't a simple env-var or IAM fix
- IAM permission denials that suggest the SAM role is missing a
  statement vs the sls role (the verification's parity diff should
  have caught this — re-check it)
- Smoke test backup fails with a CFN-level issue (e.g. a resource
  was misnamed)

Roll forward (preferred) by debugging in place. The sls stack is
gone so rollback is genuinely a re-deploy of the legacy:

```bash
# Tear down the SAM stack
sam delete --stack-name nzshm-backup-service-prod \
  --region ap-southeast-2 --no-prompts

# Wait for delete to complete
aws cloudformation wait stack-delete-complete \
  --stack-name nzshm-backup-service-prod --region ap-southeast-2

# Re-deploy sls from main (serverless.yml is still on main)
AWS_PROFILE=<aws-profile> npx sls deploy --stage prod

# Repeat steps 5 and 6 — reattach subscribers and recreate schedules.
```

The rollback window where no Lambda exists is ~5-7 min — slightly
longer than cutover because of the extra teardown step.

Update the PROD-DEPLOY-LOG entry with what failed and why, and
file a follow-up issue against PR #51's branch for the fix.

## Cutover safety rules (from PR #49 / migration doc §3a)

- **Never delete a file from the public repo until its
  replacement in `nzshm-backup-ops` has been used in at least one
  real deploy.** This runbook IS that first real use of the SAM
  template. Until it succeeds and a daily cycle has run cleanly,
  `serverless.yml` stays on `main`.
- **PROD-DEPLOY-LOG is the source of truth** for what happened.
  Update it during the cutover, not after.
- **One operator at the keyboard during cutover.** Coordination
  happens before and after, not during.

## Removing `serverless.yml` (separate follow-up PR)

After at least one full daily backup cycle has run cleanly on the
SAM-deployed Lambda (one scheduled fire, one `backup run` invocation,
no alarm fires), open a separate small PR that removes:

- `serverless.yml`
- `package.json`, `package-lock.json`, `node_modules/` (the npm
  dependency chain that supported sls)
- `.serverless/` entry from `.gitignore`
- The "legacy — kept alongside SAM during transition" comment from
  `.gitignore` (mark the migration done)
- `docs/development/lambda-deployment.md` rewritten around SAM (or
  retired if `release.md` covers it)
- The "SAM (preferred, post-#48 migration)" / "Serverless Framework
  (legacy)" split in `docs/development/release.md` flattened to
  a single SAM section
