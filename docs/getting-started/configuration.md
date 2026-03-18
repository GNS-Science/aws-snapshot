# Configuration

The CLI reads configuration from a YAML file validated against Pydantic models in
`src/nzshm_backup/config/models.py`.

## Config file location

By default the CLI looks for `backup-config.yaml` in the current directory.
Override with an environment variable:

```bash
export BACKUP_CONFIG_PATH=/path/to/my-config.yaml
```

For Lambda deployments, the config is JSON-encoded in the `BACKUP_CONFIG` environment variable.
Push/pull it with:

```bash
backup config push    # write local YAML to SSM Parameter Store
backup config pull    # read from SSM into local file
backup config show    # print current effective config
```

## Minimal example

```yaml
general:
  region: ap-southeast-2
  environment: production

sources:
  toshi:
    display_name: ToshiAPI
    s3_buckets:
      - arn: arn:aws:s3:::nzshm-toshi-api-data
        label: api
    dynamodb_tables:
      - arn:aws:dynamodb:ap-southeast-2:461564345538:table/ToshiAPI-FileTable
      - arn:aws:dynamodb:ap-southeast-2:461564345538:table/ToshiAPI-ThingTable
    source_account_id: "461564345538"
    source_account_role_arn: arn:aws:iam::461564345538:role/nzshm-backup-reader
    source_account_restore_role_arn: arn:aws:iam::461564345538:role/nzshm-backup-restore
```

## Full config reference

### `general`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `region` | string | `ap-southeast-2` | AWS region (only ap-southeast-2 currently supported) |
| `environment` | string | `production` | `production`, `staging`, or `development` |
| `tags` | dict | `{Project: NSHM, ManagedBy: backup-cli}` | Tags applied to all created resources |
| `lambda_arn` | string | `null` | ARN of the deployed backup Lambda (required for schedule targets) |
| `s3_batch_role_arn` | string | `null` | IAM role for S3 Batch Operations (required when `use_s3_batch: true`) |

### `sources.<alias>`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `display_name` | string | required | Human-readable name |
| `s3_buckets` | list | `[]` | List of `{arn, label}` objects |
| `dynamodb_tables` | list | `[]` | List of DynamoDB table ARNs |
| `dynamodb_export_format` | string | `DYNAMODB_JSON` | `DYNAMODB_JSON` or `ION` |
| `source_account_id` | string | `null` | AWS account ID owning source data (cross-account) |
| `source_account_role_arn` | string | `null` | IAM role to assume for read/backup access |
| `source_account_restore_role_arn` | string | `null` | IAM role to assume for restore operations |
| `use_s3_batch` | bool | `false` | Use S3 Batch Operations instead of per-object copy |

### `retention`

| Field | Default | Description |
|-------|---------|-------------|
| `hot_days` | 30 | Days in S3 Standard |
| `warm_days` | 120 | Days in S3 Glacier Instant (must be ≥ hot_days + 90 due to AWS constraint) |
| `cold_days` | 365 | Days in S3 Glacier Deep Archive |
| `max_age_days` | 365 | Delete objects older than this |
| `version_retention_days` | 365 | How long superseded object versions are kept; 0 = forever |

### `restore`

| Field | Default | Description |
|-------|---------|-------------|
| `default_destination_type` | `temporary` | `temporary` (auto-cleanup) or `permanent` |
| `temporary_retention_days` | 7 | Days before temporary restore bucket is deleted |
| `dynamodb_always_new_table` | `true` | Always restore DynamoDB to a new table |
| `auto_approve_threshold` | 100.0 | NZD — auto-approve restores below this cost |
| `dual_approval_threshold` | 500.0 | NZD — require two approvers above this cost |

### `notifications.ses`

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable SES email notifications |
| `source_email` | `noreply-backup@example.com` | From address |
| `recipients` | `[]` | List of recipient email addresses |

### `notifications.slack`

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable Slack notifications |
| `webhook_url_secret` | `backup-slack-webhook` | AWS Secrets Manager secret name |
| `channel` | `#nsdm-backups` | Slack channel |
| `notify_on` | backup_success, backup_failure, restore_* | Events to notify on |

### `cost_tracking`

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable cost tracking |
| `budget_alerts` | `true` | Enable AWS Budgets alerts |
| `monthly_budget` | 700.0 | NZD monthly budget threshold |
| `export_to_s3` | `null` | S3 URI to export cost reports to |

## Cross-account setup

For sources in a different AWS account than the backup Lambda:

1. Create IAM roles in the source account using `scripts/create-source-roles.py`
2. Set `source_account_id` and `source_account_role_arn` in the source config
3. For restore operations, also set `source_account_restore_role_arn`

The script writes the role ARNs back into your config automatically.

See [Account Isolation](../design/ACCOUNT_ISOLATION.md) for the full IAM trust model.
