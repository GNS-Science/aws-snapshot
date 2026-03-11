# Lambda Deployment

The backup CLI can run entirely from your terminal for on-demand use. To enable
**scheduled** backups (EventBridge → Lambda), the package must be deployed as an
AWS Lambda function using the Serverless Framework.

## Serverless Framework version

This project targets **Serverless Framework v4** (`frameworkVersion: "4"` in
`serverless.yml`).

> **v4 account requirement:** v4 prompts for a Serverless dashboard login on
> first run. For basic AWS Lambda deployments this is optional — you can skip
> the dashboard entirely. v4 has a free tier; review
> [Serverless pricing](https://www.serverless.com/pricing) if deploying at scale.

---

## Prerequisites

### 1. Node.js

Serverless Framework v3 requires Node.js 18 (Node 20 works; Node 22 is untested):

```bash
node --version    # v18.x or v20.x recommended
```

If not installed, use [nvm](https://github.com/nvm-sh/nvm) or download from nodejs.org.

### 2. Serverless Framework v4

Prefer a **local install** — version is pinned in `package.json` and the cache
is easier to reason about:

```bash
npm install                          # installs from package.json (already configured)
npx sls --version                   # should show Framework Core: 4.x
```

If you prefer a global install:
```bash
npm install -g serverless
sls --version
```

> **Cache note:** the Python requirements cache lives at
> `~/Library/Caches/serverless-python-requirements/` regardless of whether
> Serverless is installed locally or globally. `rm -rf .serverless` does **not**
> clear it — see Troubleshooting if Docker isn't being invoked.

### 3. Serverless Python Requirements plugin

```bash
sls plugin install -n serverless-python-requirements
```

### 4. Poetry (used by serverless-python-requirements to package deps)

```bash
pip install poetry    # or: brew install poetry
poetry --version
```

---

## AWS SSO credentials

Serverless Framework v3 does not understand AWS SSO profiles — it reads plain
`AWS_*` environment variables or `~/.aws/credentials` static keys.

Export your SSO session credentials as environment variables before deploying:

```bash
# Log in to SSO first (if session has expired)
aws sso login --profile your-sso-profile

# Export credentials into the current shell
eval "$(aws configure export-credentials --profile your-sso-profile --format env)"

# Verify the right account is active
aws sts get-caller-identity
```

The `eval` step sets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`AWS_SESSION_TOKEN` in your shell. These are picked up automatically by `sls deploy`.

> **Session lifetime:** SSO sessions typically last 8–12 hours. Re-run the
> `eval` line if deploy fails with an `ExpiredTokenException` after a long break.

> **`AWS_PROFILE` does not work** with Serverless v3 for SSO profiles —
> `AWS_PROFILE=your-sso-profile sls deploy` will fail with a credentials error.

---

## Deploy

The Lambda reads its config from the `BACKUP_CONFIG` environment variable
(JSON-encoded). You must export this before running `sls deploy` — Serverless
reads it from your shell and bakes it into the Lambda at deploy time.

```bash
# Convert your config YAML to JSON and export it
export BACKUP_CONFIG=$(.venv/bin/python3 -c \
  "import yaml, json; print(json.dumps(yaml.safe_load(open('backup-config.yaml'))))")

# For the sandbox config:
export BACKUP_CONFIG=$(.venv/bin/python3 -c \
  "import yaml, json; print(json.dumps(yaml.safe_load(open('backup-config.sandbox.yaml'))))")
```

Then deploy:

```bash
# Deploy to default stage (dev)
sls deploy

# Deploy to a named stage
sls deploy --stage prod
```

Serverless will:
1. Package the Python source + dependencies (via `serverless-python-requirements`)
2. Upload the zip to a staging S3 bucket
3. Create/update the CloudFormation stack with the Lambda function and IAM role
4. Print the deployed function ARN on completion

---

## Post-deploy: register the Lambda target

After deploy, copy the printed function ARN into `backup-config.yaml`:

```yaml
general:
  lambda_arn: "arn:aws:lambda:ap-southeast-2:595842668254:function:nzshm-backup-prod-backup"
```

Then wire up EventBridge rules to point at it:

```bash
export BACKUP_CONFIG_PATH=backup-config.yaml   # or sandbox variant

backup schedule add --source toshi --frequency weekly --time 14:00
backup schedule add --source ths   --frequency weekly --time 14:30
backup schedule show
```

Each `backup schedule add` call creates (or updates) the EventBridge rule **and**
registers the Lambda as the target. Until `lambda_arn` is set, the CLI creates
the rule but prints a warning that no target is registered.

---

## Teardown

```bash
sls remove --stage prod
```

This deletes the CloudFormation stack (Lambda + IAM role). EventBridge rules
created via `backup schedule add` are managed separately — remove them with:

```bash
backup schedule remove --source toshi --frequency weekly
backup schedule remove --source ths --frequency weekly
```

---

## Troubleshooting

**`sls deploy` fails with `ExpiredTokenException`**
Re-export SSO credentials:
```bash
aws sso login --profile your-sso-profile
eval "$(aws configure export-credentials --profile your-sso-profile --format env)"
```

**`sls deploy` fails with `No credentials found`**
Serverless does not read SSO profiles from `~/.aws/config`. You must use the
`eval` export approach above.

**`serverless-python-requirements` packaging fails**
Ensure Poetry is installed and `poetry.lock` is up to date:
```bash
poetry lock --no-update
sls deploy
```

**`Framework version mismatch` error**
Confirm you installed v4: `sls --version`. If it shows v3, reinstall:
```bash
npm install -g serverless
```

**Docker not being invoked — stale macOS binaries deployed (e.g. `pydantic_core._pydantic_core` missing)**
The requirements plugin caches built deps in `~/Library/Caches/serverless-python-requirements/`.
If this cache was populated before `dockerizePip: true` was set, it will be reused even when
Docker is configured, and the macOS-compiled `.so` files end up in the Lambda zip.

Clear the cache and force a rebuild:
```bash
sls requirements cleanCache          # plugin command (may not always work)
rm -rf ~/Library/Caches/serverless-python-requirements/   # nuclear option — always works
rm -rf .serverless
sls deploy --force
```

Confirm Docker ran by checking the deploy output for:
```
Docker Image: public.ecr.aws/sam/build-python3.10:latest-x86_64
Running: docker run ...
```
If those lines are absent, the cache was still used. Repeat the cache clear.

Note: `rm -rf .serverless` only removes local packaging artifacts — it does **not** clear
the requirements cache in `~/Library/Caches/`. Both may need clearing independently.
