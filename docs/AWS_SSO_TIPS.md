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
sso_account_id = 816711409078
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

### Option C — export raw credentials (for tools that don't support SSO profiles)

Some older tools and SDKs (e.g. Serverless Framework v3 and below) don't understand SSO profiles and need plain
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`:

```bash
eval "$(aws configure export-credentials --profile arkivalist-admin --format env)"
```

Credentials are temporary (~1 hour).

> **Serverless Framework v4+** uses AWS SDK v3 and supports SSO profiles natively —
> `AWS_PROFILE` alone is sufficient for `sls deploy`. The `eval` export is no longer
> needed for Serverless and should be avoided (it causes credential conflicts when
> switching profiles — see below).

---

## Switching accounts / switching back

### Safe switching after using `eval` (important)

If you previously ran `eval "$(aws configure export-credentials ...)"`, the
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` env vars are
now set. **These take precedence over `AWS_PROFILE`**, so a plain
`export AWS_PROFILE=other-profile` will silently use the wrong account.

Always clear the explicit credentials first:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=spike-admin
aws sts get-caller-identity   # confirm the switch worked
```

### Shell helper — `aws-switch` (add to `~/.zshrc` or `~/.bashrc`)

```bash
function aws-switch() {
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE="$1"
    aws sts get-caller-identity
}
```

Usage:

```bash
aws-switch spike-admin        # clears exported creds, sets profile, confirms identity
aws-switch arkivalist-admin
```

### Manual steps (without the helper)

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
| *(your default)* | `595842668254` | Spike/backup account — Lambda runs here |
| `arkivalist-admin` | `816711409078` | Arkivalist — cross-account backup demo |
| *(prod profile)* | `461564345538` | NSHM production — toshi + ths (future) |

---

## Common gotchas

**`Error: Token has expired`** — SSO session timed out. Re-run `aws sso login --profile <profile>`.

**Serverless Framework ignores SSO profile** — fixed in Serverless v4+, which uses AWS SDK v3 with native SSO support. Just set `AWS_PROFILE` and `sls deploy` works directly.

**boto3 can't resolve SSO profiles that use the `sso_session` format** — AWS CLI v2 stores
credentials differently from what botocore expects when profiles reference a separate
`[sso-session]` block without inline `sso_start_url` / `sso_region`. In this case boto3
raises `InvalidConfigError: missing required configuration: sso_start_url, sso_region`
even though the CLI works fine. Workaround: export credentials first, then run:

```bash
eval $(aws configure export-credentials --profile arkivalist-admin --format env)
python scripts/create-reader-role.py ...
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

This affects one-off admin scripts that need to talk to a non-default account directly
via boto3. The `backup` CLI itself is unaffected because it always runs as the backup
account (which has a fully-configured profile) and assumes cross-account roles via
`sts:AssumeRole` — it never creates a boto3 session from the source account's profile.

**Wrong account after switching** — always run `aws sts get-caller-identity` to confirm before running destructive or cross-account operations.

**`AWS_PROFILE` silently ignored after `eval` export** — if you previously ran
`eval "$(aws configure export-credentials ...)"`, the `AWS_ACCESS_KEY_ID` env var
is set and wins over `AWS_PROFILE`. The `backup` CLI will warn you if it detects
both set at once. Fix: `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN`
before switching profile, or use the `aws-switch` helper above.
