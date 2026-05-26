# Provisioning: `backup setup`

`backup setup` groups the **one-time provisioning** commands —
the things you run to bring a backup source into the system or to push
config-derived AWS state (IAM roles, S3 Inventory pipelines, lifecycle
policies) onto already-deployed buckets.

These are distinct from the day-to-day ops commands (`backup run`,
`backup status`, `backup health-report`) — those operate on
already-provisioned infrastructure. If you find yourself running a
`setup` command on a healthy steady-state system, you're either adding
a new source, recovering from a config drift, or rolling out a schema
change to existing buckets.

| Subcommand | Touches | When to run |
|---|---|---|
| `backup setup iam source-roles` | Source-account IAM | Once per source account, plus whenever bucket list changes |
| `backup setup iam backup-batch-role` | Backup-account IAM | Once at deploy, plus when adding S3-Batch-enabled sources |
| `backup setup inventory` | Source + backup buckets, control bucket | Once per source, plus when adding inventory to existing sources |
| `backup setup lifecycle` | Backup buckets | Once at deploy, plus when `RetentionConfig` defaults change |

All subcommands are **idempotent** — re-running rebuilds state from
the current YAML, so they're safe to invoke after any config edit. All
support `--dry-run` to preview intended changes without writing.

---

## `backup setup iam source-roles`

Creates (or rebuilds) two IAM roles in a **source account**, scoped to
the source buckets configured for one source alias:

| Role | Purpose |
|---|---|
| `nzshm-backup-reader` | Assumed by the backup Lambda for S3 sync + DynamoDB exports. Read-only. |
| `nzshm-backup-restore` | Assumed by the restore CLI and `pitr-watcher` Lambda for `RestoreTableToPointInTime`, PITR re-enable, tag management. |

Both roles trust the backup-account principal with external ID
`"nzshm-backup"`. The reader role's S3 policy is rebuilt from the
source's `s3_buckets:` list each run — add a bucket to the YAML,
re-run this command, and the policy expands to cover it.

```bash
uv run backup setup iam source-roles \
  --source toshi \
  --profile nshm-admin \
  --config backup-config.production.yaml \
  [--batch-role-arn arn:aws:iam::…:role/nzshm-s3-batch-role] \
  [--dry-run]
```

- `--profile` is the AWS CLI profile authenticated to the **source**
  account (e.g. `nshm-admin`, not `nshm-backup-admin`).
- `--batch-role-arn` is rarely needed; the default is derived from
  `general.s3_batch_role_arn` in config.

**Account context check:** the command refuses to run if the active
profile resolves to the wrong account — you'll see a clear error,
not a half-applied policy.

---

## `backup setup iam backup-batch-role`

Creates (or rebuilds) the IAM role in the **backup account** that S3
Batch Operations assumes for both backup-direction copies (source →
backup bucket) and restore-direction copies (backup bucket → source).

```bash
uv run backup setup iam backup-batch-role \
  --profile nshm-backup-admin \
  --config backup-config.production.yaml \
  [--no-write-back] \
  [--dry-run]
```

- `--profile` is the backup-account profile.
- By default the command writes the resulting role ARN back into the
  config file under `general.s3_batch_role_arn`. Pass `--no-write-back`
  to skip that (useful when running against a generated config).

Run this **once per deploy**, plus any time you add a new source with
`use_s3_batch: true` (the role's policy includes the source-bucket
list, so the policy needs to grow).

---

## `backup setup inventory`

Wires up the daily S3 Inventory pipeline for one source: configures the
inventory producer on both the source and backup buckets, points them
at the control bucket in the backup account, and sets up the partition
prefix layout that `athena_inventory.py` expects.

```bash
uv run backup setup inventory \
  --source ths \
  --source-profile nshm-admin \
  --backup-profile nshm-backup-admin \
  --config backup-config.production.yaml \
  [--control-bucket alternate-control-bucket] \
  [--control-prefix inventory] \
  [--dry-run]
```

- Needs **both** profiles — source account to configure the producer
  on the source bucket, backup account to configure the producer on
  the backup bucket and write the control-bucket policy that accepts
  inventory deliveries.
- The first inventory drops at ~02:00 UTC the next day; until then
  health-report inventory checks for this source will report
  *"no inventory data available"*.
- Re-run after the source's `s3_buckets:` list changes, or to recover
  from a control-bucket policy drift.

**Skipping**: don't run this for sources with
`inventory_enabled: false` — those are deliberately Inventory-free
(see [Health Report user guide](health-report.md#per-source-opt-out-inventory_enabled)).

---

## `backup setup lifecycle`

Re-applies the lifecycle policy (transition to GLACIER_IR at `hot_days`,
NoncurrentVersionExpiration at `version_retention_days`) to deployed
backup buckets. Required because `apply_lifecycle_policy` only runs at
**bucket creation** — a change to `RetentionConfig` defaults does not
otherwise propagate to existing buckets.

```bash
uv run backup setup lifecycle \
  --source all \
  --config backup-config.production.yaml \
  [--dry-run]
```

- `--source <alias|all>` selects sources. `all` walks every configured
  source.
- `--dry-run` prints the bucket list it would touch and the full
  lifecycle JSON it would push — useful as a first pass to verify
  bucket names match deployed reality.
- Walks each source's `s3_buckets:` entries plus the DynamoDB export
  bucket if the source has `dynamodb_tables:`.

Run after:

- Any change to `RetentionConfig` (`hot_days`, `version_retention_days`)
- Any ADR that changes the lifecycle policy shape (e.g. ADR-006)
- Adopting an existing un-managed bucket (the bucket might have an
  inherited policy or none at all)

The command derives `LifecycleConfig` from `config.retention` so a YAML
edit followed by this command pushes the new policy across all buckets
in one go. See ADR-006 deploy log entry (`docs/PROD-DEPLOY-LOG.md`
Step 18) for a worked example.

---

## Typical first-time deploy order

For a fresh source, the order is:

```bash
# 1. Source-account IAM
uv run backup setup iam source-roles \
  --source <alias> --profile nshm-admin \
  --config backup-config.production.yaml

# 2. Backup-account IAM (only if any source uses S3 Batch)
uv run backup setup iam backup-batch-role \
  --profile nshm-backup-admin \
  --config backup-config.production.yaml

# 3. First backup (creates backup bucket + applies lifecycle on creation)
uv run backup run --source <alias> --config backup-config.production.yaml

# 4. Inventory pipeline (only if inventory_enabled is true for this source)
uv run backup setup inventory \
  --source <alias> \
  --source-profile nshm-admin --backup-profile nshm-backup-admin \
  --config backup-config.production.yaml

# 5. (Optional) re-apply lifecycle if you've since changed RetentionConfig
uv run backup setup lifecycle --source <alias> \
  --config backup-config.production.yaml
```

After step 3, run `backup check --source <alias>` to verify IAM and
bucket reachability. After step 4, wait ~24h for the first inventory
drop before relying on the health report's inventory-age / divergence
signals.

---

## Related

- [Backup Operations](backup.md) — day-to-day `backup run` and `backup status`
- [Health Report](health-report.md) — covers the signals each setup step enables
- [Operator Cheatsheet](../operations/cheatsheet.md) — quick task lookup
- [Production Deploy Log](../PROD-DEPLOY-LOG.md) — chronological deploy history including worked examples
