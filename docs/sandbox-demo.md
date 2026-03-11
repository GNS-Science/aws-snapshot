# Sandbox Demo Guide

End-to-end test of the Phase 2 feature set against real AWS using the sandbox
account (595842668254). No production resources are touched.

## Prerequisites

> **AWS SSO users:** Serverless Framework does not read SSO profiles directly.
> Export credentials before running `sls deploy`:
> ```bash
> aws sso login --profile your-sso-profile
> eval "$(aws configure export-credentials --profile your-sso-profile --format env)"
> ```
> See [Lambda Deployment](../development/lambda-deployment.md) for full setup
> instructions including Serverless Framework installation.

```bash
# AWS CLI configured for sandbox account
aws sts get-caller-identity   # should show 595842668254

# Python venv with package installed
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
backup --help                 # should display command tree
```

## Step 1 — Create sandbox resources

```bash
scripts/sandbox_setup.sh setup
```

Creates in sandbox account:

| Resource | Name | Notes |
|----------|------|-------|
| S3 | `nzshm22-toshi-api-sandbox` | 5 seeded objects, mirrors `nzshm22-toshi-api-prod` |
| S3 | `ths-dataset-sandbox` | 3 seeded objects, mirrors `ths-dataset-prod` |
| DynamoDB | `ToshiFileObject-PROD` | PITR enabled, 3 items |
| DynamoDB | `ToshiIdentity-PROD` | PITR enabled, 3 items |
| DynamoDB | `ToshiTableObject-PROD` | PITR enabled, 3 items |
| DynamoDB | `ToshiThingObject-PROD` | PITR enabled, 3 items |

Also writes `backup-config.sandbox.yaml` with the sandbox account ID and ARNs
pre-filled.

## Step 2 — Set config path

```bash
export BACKUP_CONFIG_PATH=backup-config.sandbox.yaml
```

All subsequent `backup` commands pick this up automatically via the
`BACKUP_CONFIG_PATH` env var. No need to rename files.

## Step 3 — Dry run (no AWS writes)

```bash
backup --dry-run run --source toshi
```

Expected output:
```
[DRY RUN] Export initiated: ToshiFileObject-PROD → skipped
[DRY RUN] Export initiated: ToshiIdentity-PROD → skipped
[DRY RUN] Export initiated: ToshiTableObject-PROD → skipped
[DRY RUN] Export initiated: ToshiThingObject-PROD → skipped

Backup completed successfully
[DRY RUN] Would copy N objects (X.XX MB)
```

## Step 4 — Live backup run

```bash
backup run --source toshi
```

This will:
1. Create `nzshm22-toshi-api-sandbox-backup-ap-southeast-2-595842668254` (S3 backup bucket)
2. Sync the 5 source objects into it
3. Create `nzshm-dynamo-backup-toshi-ap-southeast-2-595842668254` (DynamoDB export bucket)
4. Initiate PITR exports for all 4 tables — each returns an `ExportArn`

> **Note — sandbox vs production S3 output:**
> The sandbox config uses `use_s3_batch: false` (the default), so the S3 sync runs
> synchronously and reports immediately:
> ```
> Copied 5 objects (0.00 MB) in 1.2s
> ```
> In **production**, `toshi` will have `use_s3_batch: true` (required for ~8M objects).
> The output there will be:
> ```
> Batch job submitted: abc1234 (N objects)
> ```
> The copy then runs asynchronously in AWS. Monitor progress with:
> ```bash
> aws s3control describe-job --account-id ACCOUNT_ID --job-id JOB_ID --region ap-southeast-2
> ```
> See `docs/architecture/s3-batch-operations.md` for full details.

```bash
# Back up ths source too
backup run --source ths

# Or both at once
backup run --source all
```

## Step 5 — Verify backup state

```bash
scripts/sandbox_setup.sh status
```

Shows source buckets, DynamoDB tables (item count + PITR status), backup output
buckets (object count), and any EventBridge rules.

## Step 6 — Schedule management

```bash
# List rules (empty at this point)
backup schedule show

# Create weekly rule for toshi — 14:00 UTC = 02:00 NZST (Sun)
backup schedule add --source toshi --frequency weekly --time 14:00

# Create daily rule
backup schedule add --source toshi --frequency daily --time 13:00

# Create weekly rule for ths
backup schedule add --source ths --frequency weekly --time 14:30

# List all rules
backup schedule show

# Disable / re-enable
backup schedule disable --source toshi
backup schedule show
backup schedule enable --source toshi --frequency weekly
backup schedule show
```

Note: Rules are created in ENABLED state. No Lambda target is registered until
`lambda_arn` is set in the config — see Step 6a below.

## Step 6a — Deploy Lambda (optional, needed for scheduled triggers)

To test EventBridge actually invoking a backup, deploy the Lambda and register
it as the rule target. See [Lambda Deployment](../development/lambda-deployment.md)
for full instructions. Summary:

```bash
# Export SSO credentials first
eval "$(aws configure export-credentials --profile your-sso-profile --format env)"

# Export config as JSON (required — sls reads it from the shell environment)
export BACKUP_CONFIG=$(.venv/bin/python3 -c \
  "import yaml, json; print(json.dumps(yaml.safe_load(open('backup-config.sandbox.yaml'))))")

# Deploy
sls deploy --stage sandbox

# Copy the printed ARN into backup-config.sandbox.yaml:
#   general:
#     lambda_arn: "arn:aws:lambda:ap-southeast-2:595842668254:function:nzshm-backup-sandbox-backup"

# Re-run schedule add to register the target
backup schedule add --source toshi --frequency hourly --time 00:05
backup schedule add --source toshi --frequency minutely   # fires every minute — demo use only

# Watch it fire, then clean up
backup schedule remove --source toshi --frequency minutely
```

## Step 7 — Teardown

```bash
scripts/sandbox_setup.sh teardown
```

Deletes (with confirmation prompt):
- Source S3 buckets and all objects
- Backup output S3 buckets and all objects (lifecycle + delete-protection policies
  removed first)
- All 4 DynamoDB tables
- Any `nzshm-backup-*` EventBridge rules
- `backup-config.sandbox.yaml`

---

## Production resource reference

| Resource | Production name | Prod account |
|----------|----------------|--------------|
| S3 | `nzshm22-toshi-api-prod` | 461564345538 |
| S3 | `ths-dataset-prod` | 461564345538 |
| DynamoDB | `ToshiFileObject-PROD` | 461564345538 |
| DynamoDB | `ToshiIdentity-PROD` | 461564345538 |
| DynamoDB | `ToshiTableObject-PROD` | 461564345538 |
| DynamoDB | `ToshiThingObject-PROD` | 461564345538 |

See `backup-config.example.yaml` for the full production config template.

---

## Troubleshooting

**Wrong account error:**
```
[ERROR] Expected sandbox account 595842668254 but got XXXXXXXXXXXX
```
Switch AWS credentials to the sandbox account profile before running.

**`backup` command not found:**
```bash
source .venv/bin/activate
pip install -e .
```

**DynamoDB export shows FAILED in status:**
PITR exports are async — the `INITIATED` status from `backup run` is correct.
Check export progress in the AWS console under DynamoDB → Exports, or poll:
```bash
aws dynamodb list-exports --region ap-southeast-2
```

**Backup bucket already exists error (S3):**
The S3 backup module ABENDs if a backup bucket exists but was not created by
this tool (i.e. lacks the `ManagedBy: nzshm-backup` tag). Buckets created by a previous `backup run` are recognised and reused — the sync
is additive only (new/changed objects copied, nothing deleted), so existing
backup data is preserved and accumulates until the lifecycle policy expires it. If you see this error
for an unexpected bucket, run `scripts/sandbox_setup.sh teardown` to clean up,
then re-run setup.
