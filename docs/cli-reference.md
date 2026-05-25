# CLI Reference

## Command tree

Use this as a quick map of the CLI surface. Detailed options/arguments for each
command are listed in the generated reference below.

```text
backup
├── check
├── schedule
│   ├── show
│   ├── health
│   ├── add
│   ├── remove
│   ├── enable
│   └── disable
├── setup
│   ├── inventory
│   └── iam
│       ├── source-roles
│       └── backup-batch-role
├── run
├── restore
├── test
├── status
├── events
├── report
├── costs
├── health-report
│   ├── run
│   └── preview
└── config
    ├── show
    ├── validate
    ├── push
    └── pull
```

## Full command reference

::: mkdocs-click
    :module: nzshm_backup.cli
    :command: click_app
    :depth: 1
    :style: table
    :list_subcommands: true

---

## `backup health-report` — daily health report

Implements the slow-path half of ADR-005 (the fast-path Lambda-errors
alarm lives separately in CloudWatch). Same code path the scheduled
Lambda runs in production; running it from the CLI is how operators
verify the report end-to-end after deploy.

Full operator guide: [Daily Health Report](user-guide/health-report.md).

### `backup health-report run`

Build the daily report and print it. Optionally deliver via the
configured Slack webhook and SNS email subscription.

```bash
# Print only — no delivery; runs the slow restore tests.
uv run backup health-report run

# Print + deliver via enabled channels.
uv run backup health-report run --send

# Skip the (~30s per source) restore tests; status + freshness + delta only.
uv run backup health-report run --dry-run

# Force rotation as if today were Monday (testing rotation logic).
uv run backup health-report run --weekday 0

# Override the SNS topic (auto-resolved from session + stage otherwise).
uv run backup health-report run --send \
    --topic-arn arn:aws:sns:ap-southeast-2:737696831915:nzshm-backup-reports-prod
```

Flags:

| Flag | Default | Effect |
|---|---|---|
| `--send` | off | Deliver via Slack (if `notifications.slack.enabled`) and SNS (if `notifications.reports.email.enabled`). Channel failures are independent. |
| `--dry-run` | off | Skip the restore-test calls. Useful for iterating on the formatter or verifying status/freshness/delta without the slow path. |
| `--weekday <0–6>` | today's NZ weekday | Override the rotation lookup. 0=Mon … 6=Sun. |
| `--topic-arn <arn>` | auto-resolved | Override the SNS reports topic ARN. Resolution order: this flag → `$BACKUP_REPORTS_TOPIC_ARN` → constructed from session region + STS account + `--stage`. |
| `--stage <name>` | `prod` | Used only when constructing the topic ARN (matches the `nzshm-backup-reports-{stage}` name in `serverless.yml`). |

Output format: the same plain-text body that ships via SNS, preceded by
the subject line. When `--send` is set, a trailing Delivery section
prints per-channel status.

### `backup health-report preview`

Sugar for `run --dry-run`. Same output, no delivery, no restore tests.
Useful for fast iteration without burning Athena queries on the slow
sample-and-verify path.

```bash
uv run backup health-report preview
uv run backup health-report preview --weekday 4
```

### Required local permissions

When running with `--send` from a laptop (not the Lambda):

- `s3:ListBucket`, `s3:GetObject`, `s3:CopyObject`, `s3:PutObject`,
  `s3:CreateBucket`, `s3:DeleteBucket` (temp restore-test bucket)
- `sts:AssumeRole` (cross-account source-bucket reads)
- `athena:StartQueryExecution`, `athena:GetQueryExecution`,
  `glue:GetTable`/`GetPartitions` (count_delta + restore sampling)
- `dynamodb:DescribeContinuousBackups` (PITR check)
- `sns:Publish` on the reports topic
- `secretsmanager:GetSecretValue` on `backup-slack-webhook-*`

The deployed Lambda role already has all of these (see `serverless.yml`).
