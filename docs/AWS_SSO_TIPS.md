# AWS SSO — Tips & Tricks

## Finding your configured profiles

```bash
# List all profiles
aws configure list-profiles

# Inspect the config file directly — clearer for SSO profiles
cat ~/.aws/config
```

SSO profiles in `~/.aws/config` look like:

```ini
[profile arkivalist-admin]
sso_start_url = https://your-org.awsapps.com/start
sso_region = ap-southeast-2
sso_account_id = 456789012345
sso_role_name = AdministratorAccess
region = ap-southeast-2
```

---

## Creating a new SSO profile

### Interactive wizard (recommended for first time)

```bash
aws configure sso
```

Prompts for SSO start URL, SSO region, account (shows a list), role, and local
profile name. Writes the result to `~/.aws/config`.

### Manual (faster when adding multiple accounts)

Paste a stanza directly into `~/.aws/config`:

```ini
[profile my-new-profile]
sso_start_url = https://your-org.awsapps.com/start
sso_region = ap-southeast-2
sso_account_id = 123456789012
sso_role_name = AdministratorAccess
region = ap-southeast-2
```

---

## Starting a session

```bash
# Login — opens browser once per SSO session, covers ALL profiles in the same org
aws sso login --profile arkivalist-admin

# Verify you're in the right account
aws sts get-caller-identity --profile arkivalist-admin
```

One `sso login` covers all profiles sharing the same `sso_start_url` — you don't
need to login separately for each account.

---

## Using a profile

### Option A — inline `--profile` (one-off commands)

```bash
aws iam list-roles --profile arkivalist-admin
```

### Option B — `AWS_PROFILE` env var (whole shell session)

```bash
export AWS_PROFILE=arkivalist-admin
aws sts get-caller-identity   # no --profile needed
python scripts/create-reader-role.py ...
```

### Option C — export raw credentials (required for tools that don't read SSO profiles)

Some tools (Serverless Framework, some older SDKs) don't understand SSO profiles
and need plain `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`:

```bash
eval "$(aws configure export-credentials --profile arkivalist-admin --format env)"
```

Credentials are temporary (~1 hour). This is required for `sls deploy`.

---

## Switching accounts / switching back

```bash
# Switch
export AWS_PROFILE=arkivalist-admin

# Switch back
export AWS_PROFILE=your-normal-profile

# Or clear entirely (falls back to default profile or instance role)
unset AWS_PROFILE

# If you exported raw credentials, clear them explicitly
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

Always verify after switching:

```bash
aws sts get-caller-identity
```

---

## Account reference for this project

| Profile | Account ID | Purpose |
|---------|-----------|---------|
| *(your default)* | `345678901234` | Spike/backup account — Lambda runs here |
| `arkivalist-admin` | `456789012345` | Arkivalist — cross-account backup demo |
| *(prod profile)* | `210987654321` | NSHM production — toshi + ths (future) |

---

## Common gotchas

**`Error: Token has expired`** — SSO session timed out. Re-run `aws sso login --profile <profile>`.

**Serverless Framework ignores SSO profile** — use Option C (export raw credentials) before `sls deploy`.

**boto3 / backup CLI ignores `AWS_PROFILE`** — boto3 *does* support SSO profiles natively; Option B works fine for `backup run` and scripts. Only Option C is needed for non-AWS tools.

**Wrong account after switching** — always run `aws sts get-caller-identity` to confirm before running destructive or cross-account operations.
