# Lambda Deployment

The backup CLI runs entirely from your terminal for on-demand use. To
enable **scheduled** backups (EventBridge → Lambda), the package must
be deployed as an AWS Lambda function via AWS SAM.

This document covers prerequisites and the day-to-day deploy command.
For the side-stack verification procedure see
[SAM deploy verification](sam-deploy-verification.md). For the
historical one-time cutover from Serverless Framework see
[SAM cutover runbook](sam-cutover-runbook.md).

---

## Prerequisites

### 1. uv-managed Python environment

SAM CLI is a dev dependency declared in `pyproject.toml`. The standard
`make sync` (or `uv sync --all-extras`) brings it in:

```bash
make sync
uv run sam --version    # should print "SAM CLI, version 1.x"
```

No separate `pip install` step needed.

### 2. Docker

Required by `sam build --use-container`, which mirrors the
`dockerizePip` behaviour that `serverless-python-requirements` provided
previously. Start Docker Desktop (or your local docker daemon):

```bash
docker info >/dev/null   # should succeed
```

The first `sam build` after a clean install pulls
`public.ecr.aws/sam/build-python3.10:latest-x86_64` (~600 MB). Subsequent
builds reuse the image.

### 3. AWS credentials

SAM honours the standard AWS SDK credential-chain. The recommended
pattern is to export SSO credentials into the current shell:

```bash
aws sso login --profile <your-sso-profile>
eval "$(aws configure export-credentials --profile <your-sso-profile> --format env)"
aws sts get-caller-identity   # confirm correct account
```

`AWS_PROFILE` alone does not work with SSO profiles — you must `eval`
the credentials into env vars.

### 4. samconfig.toml

`samconfig.toml` holds deploy parameters (stack name, region, parameter
overrides). It is `.gitignore`d (each install carries its own). A
committed example exists at `samconfig.example.toml`:

```bash
cp samconfig.example.toml samconfig.toml
$EDITOR samconfig.toml   # update stack_name, region, parameter_overrides
```

The key parameters in `parameter_overrides`:

- `Stage` — `prod`, `sandbox`, or whatever stage convention your
  install uses
- `ServiceName` — resource-name prefix (default `nzshm-backup-service`)
- `BatchRoleArn` — the IAM role passed to S3 Batch Operations
  (`iam:PassRole` target)
- `SlackWebhookSecretName` — Secrets Manager secret holding the Slack
  webhook URL (default `backup-slack-webhook`)

---

## Deploy

Build, then deploy:

```bash
make sam-build      # uv export → requirements.txt + sam build --use-container
sam deploy          # uses samconfig.toml; confirm_changeset=true gives a Y/n pause
```

`make sam-build` wraps two steps: it generates `requirements.txt` from
`uv.lock` (the SAM container doesn't have `uv`), then runs
`sam build --use-container`. The build produces three Lambda artefacts
in `.aws-sam/build/`.

`sam deploy` creates a CFN changeset, prompts you to review it (resource
adds/changes/deletes), and applies on confirmation. Resource-level
duration is typically 2-4 minutes for an update; 5-7 minutes for a fresh
deploy.

> **Note: `.venv` and `sam build --use-container`.** The SAM build
> container mounts the repo as a read-only volume and walks it to copy
> sources. Broken symlinks (e.g. `.venv/bin/python3` pointing at the
> host's Python install) cause the copy step to fail. The standard
> workaround is to move `.venv` out of the source tree for the duration
> of the build:
>
> ```bash
> mv .venv /tmp/saved-venv && make sam-build && sam deploy && mv /tmp/saved-venv .venv
> ```

---

## Stack contents

A successful deploy produces 15 CloudFormation resources:

| Resource | Purpose |
|---|---|
| 3 × `AWS::Lambda::Function` | `backup`, `pitr-watcher`, `alarm-bridge` |
| 1 × `AWS::IAM::Role` | shared execution role for all three functions |
| 1 × `AWS::Logs::LogGroup` | explicit log group for the backup function (90-day retention) |
| 2 × `AWS::SNS::Topic` | `nzshm-backup-alerts-<stage>`, `nzshm-backup-reports-<stage>` |
| 3 × `AWS::CloudWatch::Alarm` | uncaught-error backstop, ERROR-line log-metric, pitr-watcher errors |
| 1 × `AWS::Logs::MetricFilter` | counts `[ERROR]` log lines into a custom metric |
| 1 × `AWS::Events::Rule` | the pitr-watcher's 5-min schedule (disabled by default) |
| 2 × `AWS::Lambda::Permission` | SNS-invoke + EventBridge-invoke grants |
| 1 × `AWS::SNS::Subscription` | alarm-bridge Lambda subscribed to alerts topic |

Per-source backup schedules (one EventBridge rule per source, daily fire
at 09:45 local) are **not** in the CFN stack — they're created and
managed via the `backup schedule add` CLI command, which writes
EventBridge rules directly and sets the Lambda permission for each.

---

## Post-deploy

After a fresh deploy or a stack replacement, three CLI commands tie
together the operational pieces the CFN stack doesn't manage:

```bash
backup notifications apply
# Reconciles SNS subscribers on the alerts + reports topics against
# the email lists in backup-config.<stage>.yaml. Subscribers get a
# confirmation email per topic.

backup schedule add --source <alias> --frequency daily --time "09:45 NZST"
# (Repeat per source. Creates the EventBridge rule + Lambda permission.)

backup health-report preview
# Dry-run the daily report against current state. Useful for confirming
# the deployed Lambda can reach SSM config, source buckets, etc.
```

---

## Teardown

```bash
sam delete --stack-name <your-stack-name>
```

Removes the 15 CFN resources. Does **not** remove the per-source
EventBridge schedules (they're CLI-managed, not CFN-managed). Clean those
up with `backup schedule remove --source <alias> --frequency daily` per
source if you want a fully blank slate.

---

## Troubleshooting

**`sam build` fails with "No such file or directory: /tmp/samcli/source/.venv/bin/python3"**
Move `.venv` out of the source tree (see note above). The host venv has
broken symlinks from the container's perspective.

**`sam deploy` fails with "Parameter 'Stage' must be one of AllowedValues"**
Check `samconfig.toml`'s `parameter_overrides` is a **single-line** string.
TOML's `"""` multi-line form preserves embedded newlines, which CFN rejects.

**`sam deploy` fails with "Resource handler returned message: log group does not exist"**
Should not happen — the SAM template declares the log group explicitly.
If it does, the deploy may be using a stale `.aws-sam/build/` directory.
`rm -rf .aws-sam && make sam-build` and retry.

**Stack stuck in `ROLLBACK_COMPLETE`**
CFN can't update a failed-create stack — delete it first:

```bash
sam delete --stack-name <your-stack-name> --no-prompts
```

Then re-attempt `sam deploy`.

**Deployed Lambda errors at cold-start with `Runtime.ImportModuleError: Unable to import module 'aws_snapshot.lambda_handler': No module named 'aws_snapshot'`**
The deployed artefact is the source tree itself, not the Makefile's clean
build output. Almost always caused by `template_file = "sam.yaml"` being
placed under `[default.global.parameters]` in `samconfig.toml`. A *global*
`template_file` applies to `sam deploy` too, overriding its default of
picking up `.aws-sam/build/template.yaml` (the build-output template, which
points each function at its own clean build dir). `sam deploy` then
uploads from the source `CodeUri: ./` — packaging the entire repo into the
Lambda artefact. Since the package lives at `src/aws_snapshot/`, not at
artefact root, the import fails.

Fix: move `template_file = "sam.yaml"` to `[default.build.parameters]`
(see `samconfig.example.toml`), rebuild (`rm -rf .aws-sam && make sam-build`),
and redeploy. Production incident on 2026-06-26 13:00 NZST traced to this
trap (PR #55 introduced the global setting; PR #56 fixed it).
