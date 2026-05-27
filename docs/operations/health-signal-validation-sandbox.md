# Health Signal Validation — Sandbox Runbook

Drive known-shape scenarios on two toy backup sources to validate the
ADR-009 signal classifications end-to-end against real AWS infrastructure.
Use this runbook when:

- Classification thresholds change (`_classify_source` in `health_report.py`)
- New signals are added
- Verifying a claim in [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md)
- After a significant refactor of `divergence_counts`, `count_delta`, or
  `inventory_health_for_bucket_pair`

The 24h S3 Inventory cadence is the unavoidable iteration floor; this
runbook is designed to stack 4+ scenarios per cycle so a full sweep is
~48h, not weeks.

## Topology

Two toy sources live in `backup-config.production.yaml` alongside real
production sources. Both are backed up by the regular Lambda schedule
and included in the daily health report.

| Source | `inventory_enabled` | Steady-state | Purpose |
|---|---|---|---|
| `toy-inv` | `true` (default) | GREEN | Exercises all class 1/2/3 signal paths |
| `toy-noinv` | `false` | GREEN (restore-test dominant) | Exercises the floor mode — no Inventory, no Athena, restore test as the only red signal |

Both sources are visible to subscribers (display names prefixed
`[SANDBOX]`). Either may flip colours briefly during scenario drills —
disregard for on-call escalation while validation is in progress.

Bucket layout (single AWS Organization, two accounts):

```
Source account 210987654321          Backup account 123456789012
─────────────────────────────        ──────────────────────────────
nzshm22-toy-inv-source     ────►     bb-toy-inv-s3-src-…-210987654321
nzshm22-toy-noinv-source   ────►     bb-toy-noinv-s3-src-…-210987654321
```

(Backup bucket names embed `source_account_id` per `backup_engine.py:72,81`.)

## One-time setup

### 0. Shell prep (`.env`-aware)

This runbook assumes `BACKUP_CONFIG_PATH=backup-config.production.yaml`
is set in your project `.env`. `uv run` loads that into the process
environment before Python starts, so the `uv run backup …` commands
below pick the right config without a `--config` flag.

```bash
grep BACKUP_CONFIG_PATH .env
# → BACKUP_CONFIG_PATH=backup-config.production.yaml
```

For commands that touch the **backup account** (`backup run`,
`backup setup lifecycle`, `backup config push`), eval-export
`<aws-profile>` credentials and unset `AWS_PROFILE` to keep
boto3's credential chain unambiguous:

```bash
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"
unset AWS_PROFILE
```

The `backup setup iam source-roles` and `backup setup inventory`
commands take explicit `--source-profile` / `--profile` arguments and
construct boto3 sessions via `profile_name=…`, which bypasses the
exported env vars — no harm running them with the eval already in
place, but no benefit either.

### 1. Create source buckets

In account `210987654321` (source) under `nshm-admin` profile.

> ⚠ **Gotcha:** if you've recently `eval`-exported `<aws-profile>`
> credentials (Step 0), those env vars override `AWS_PROFILE`. Without
> an explicit `--profile nshm-admin` on each `aws` command, the buckets
> would silently get created in the **backup** account
> (`123456789012`), the cross-account `Condition:
> s3:ResourceAccount=="210987654321"` on the reader role would fail at
> first `backup run`, and you'd have to delete + recreate. Pin every
> `aws` call to `--profile nshm-admin` (or `unset AWS_ACCESS_KEY_ID
> AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` first).

```bash
aws s3api create-bucket --bucket nzshm22-toy-inv-source \
  --region ap-southeast-2 \
  --create-bucket-configuration LocationConstraint=ap-southeast-2 \
  --profile nshm-admin
aws s3api create-bucket --bucket nzshm22-toy-noinv-source \
  --region ap-southeast-2 \
  --create-bucket-configuration LocationConstraint=ap-southeast-2 \
  --profile nshm-admin

# Populate each with ~50 small text files
for B in nzshm22-toy-inv-source nzshm22-toy-noinv-source; do
  for i in $(seq -w 1 50); do
    echo "toy file $i for $B (created $(date -u +%FT%TZ))" \
      | aws s3 cp - "s3://$B/data/file-$i.txt" --profile nshm-admin
  done
done

# Verify ownership before proceeding — both names should appear here:
aws s3api list-buckets --profile nshm-admin \
  --query "Buckets[?contains(Name, 'toy')].Name" --output text
```

### 2. Add to production config

Append to `backup-config.production.yaml` under `sources:`:

```yaml
  toy-inv:
    display_name: "[SANDBOX] toy-inv (Inventory enabled)"
    s3_buckets:
      - arn: arn:aws:s3:::nzshm22-toy-inv-source
        label: src
    source_account_role_arn: arn:aws:iam::210987654321:role/nzshm-backup-reader
    source_account_restore_role_arn: arn:aws:iam::210987654321:role/nzshm-backup-restore
    source_account_id: '210987654321'
    use_s3_batch: false
    # inventory_enabled: true   # default — left implicit

  toy-noinv:
    display_name: "[SANDBOX] toy-noinv (no Inventory)"
    s3_buckets:
      - arn: arn:aws:s3:::nzshm22-toy-noinv-source
        label: src
    source_account_role_arn: arn:aws:iam::210987654321:role/nzshm-backup-reader
    source_account_restore_role_arn: arn:aws:iam::210987654321:role/nzshm-backup-restore
    source_account_id: '210987654321'
    use_s3_batch: false
    inventory_enabled: false      # the whole point of this source
```

### 3. Extend source-account IAM

The existing `nzshm-backup-reader` role in `210987654321` needs read
access to the new buckets. Re-run the source-roles setup script — it's
idempotent and rebuilds the policy from the YAML:

```bash
uv run backup setup iam source-roles \
  --source toy-inv --profile nshm-admin

uv run backup setup iam source-roles \
  --source toy-noinv --profile nshm-admin
```

### 4. First backup + lifecycle apply

Run before Inventory setup — the Inventory pipeline's
`PutBucketInventoryConfiguration` call on the backup-side bucket
requires the bucket to exist, and `backup run` is what creates it
(plus applies lifecycle on creation):

```bash
uv run backup run --source toy-inv
uv run backup run --source toy-noinv
uv run backup setup lifecycle --source toy-inv
uv run backup setup lifecycle --source toy-noinv
```

### 5. Inventory setup (toy-inv only)

```bash
uv run backup setup inventory --source toy-inv \
  --source-profile nshm-admin --backup-profile <aws-profile>
```

Do **not** run this for `toy-noinv` — the whole point is to exercise
the no-Inventory floor.

### 6. Push config to SSM and redeploy

The Lambda reads config from SSM Parameter Store. If only the YAML
changed, a `config push` is sufficient; if Lambda code changed (e.g.
when `inventory_enabled` is being shipped for the first time), redeploy:

```bash
uv run backup config push --stage prod
# If code changed: AWS_PROFILE=<aws-profile> npx sls deploy --stage prod
```

### 7. Subscriber heads-up

Post in the operations Slack channel before the next 14:30 NZST report:

> Heads-up: adding two toy sources `[SANDBOX] toy-inv` and `[SANDBOX] toy-noinv`
> to the daily health report for ADR-009 signal validation.
>
> - `toy-inv` runs full Inventory + Athena + restore-test path; should be
>   steady GREEN. Will flip colours during scenario drills (expected).
> - `toy-noinv` has `inventory_enabled: false`; floor mode — restore
>   test is the only red signal. Carries an `ℹ` info line on every row.
> - Daily headline reflects real-prod sources accurately; sandbox sources
>   are not suppressed but normally won't pull it red.
> - Disregard sandbox rows for on-call escalation.
> - Sandbox tear-down planned for: <date>. Runbook:
>   `docs/operations/health-signal-validation-sandbox.md`.

### 8. Wait 24h

S3 Inventory drops at ~02:00 UTC daily. First inventory for `toy-inv`
is available the following morning NZST. Until then, `toy-inv` will
show "no inventory data available" and red. After the first drop it
should turn green. `toy-noinv` is green from the first report cycle —
it has no inventory dependency.

## Scenario matrix

Each row is a single manipulation made before ~02:00 UTC on day D₀; the
expected signal lands in the report at ~14:30 NZST on day D₁ (next
afternoon). Stack scenarios per cycle to keep iteration to ~48h total.

### Cycle 1 — class-1 RED signals (the ones that matter)

| # | Action (D₀, before 02:00 UTC) | Source | Expected D₁ report |
|---|---|---|---|
| 1 | `aws s3api delete-object --bucket bb-toy-inv-s3-src-…-210987654321 --key data/file-01.txt --version-id <v>` (admin override of the no-delete bucket policy) | `toy-inv` | ⚠ class-1 RED: "backup is missing 1 source keys" |
| 2 | Strip `s3:GetObject` from the source-account `nzshm-backup-reader` role for `nzshm22-toy-inv-source` | `toy-inv` | ⚠ class-1 RED on restore-test day for `toy-inv`: "restore test exception: AccessDenied" |
| 3 | Disable the daily inventory producer for `toy-inv` (delete the S3 Inventory configuration on the source bucket) | `toy-inv` | ⚠ class-1 RED: "no inventory data available" (since `inventory_enabled` is true) |
| 4 | Strip `s3:GetObject` from source role for `nzshm22-toy-noinv-source` | `toy-noinv` | ⚠ class-1 RED on restore-test day for `toy-noinv`: "restore test exception". Confirms restore-test red still fires when inventory_enabled=false. |

### Cycle 2 — class-2 informational ℹ + class-3 yellow

After restoring cycle-1 state (see *Tearing down a scenario*, below),
wait one cycle for greens, then:

| # | Action (D₀, before 02:00 UTC) | Source | Expected D₁ report |
|---|---|---|---|
| 5 | `aws s3 rm s3://nzshm22-toy-inv-source/data/file-02.txt` (source-side delete; backup retains it per ADR-006) | `toy-inv` | ℹ class-2: "backup has 1 orphans (source-side deletions retained per ADR-006)". Row stays GREEN. |
| 6 | Add 10 new files: `for i in $(seq 51 60); do echo extra | aws s3 cp - s3://nzshm22-toy-inv-source/data/file-$i.txt; done` | `toy-inv` | ℹ class-2: "source grew by 10 objects vs yesterday (+20.0%)". Row stays GREEN. |
| 7 | Skip the daily backup for `toy-inv`: `backup schedule disable --source toy-inv` | `toy-inv` | ⚠ class-3 YELLOW once inventory_age crosses 30h threshold. Row turns YELLOW, not RED. |
| 8 | (No action) | `toy-noinv` | Steady GREEN with the `ℹ` "inventory disabled" info line. Confirms class-2 info line renders correctly for opted-out sources. |

### Cycle 3 — recovery / GREEN verification

Tear down all manipulations on D₂ morning, wait one cycle to D₃.
Expected: both toys GREEN, real-prod sources unaffected throughout.

## Tearing down a scenario

For each cycle-1 manipulation:

| # | Recovery action |
|---|---|
| 1 | `aws s3 cp s3://nzshm22-toy-inv-source/data/file-01.txt s3://bb-toy-inv-s3-src-…-210987654321/data/file-01.txt` |
| 2 | Re-run `backup setup iam source-roles --source toy-inv` to restore the policy |
| 3 | Re-run `backup setup inventory --source toy-inv` to reinstall the Inventory config |
| 4 | Re-run `backup setup iam source-roles --source toy-noinv` |

For cycle-2:

| # | Recovery action |
|---|---|
| 5 | Re-copy `file-02.txt` to source. Class-2 ℹ orphan clears on the next divergence scan. |
| 6 | Delete the 10 added files. Class-2 ℹ delta clears on the next `count_delta`. |
| 7 | `backup schedule enable --source toy-inv` and `backup run --source toy-inv` to refresh. Inventory freshness recovers on the next Inventory drop. |
| 8 | None required. |

After each tear-down, wait one cycle for the report to confirm GREEN
before starting the next cycle.

## Full teardown of toy sources

When validation is complete:

```bash
# 1. Remove from config
$EDITOR backup-config.production.yaml   # delete toy-inv + toy-noinv sources
# (or revert the TMP commit: git revert <toy-yaml-commit-sha>)

# 2. Push to SSM
uv run backup config push --stage prod

# 3. Empty + delete buckets (source + backup sides)
for B in nzshm22-toy-inv-source nzshm22-toy-noinv-source; do
  AWS_PROFILE=nshm-admin aws s3 rm "s3://$B" --recursive
  AWS_PROFILE=nshm-admin aws s3api delete-bucket --bucket "$B"
done

for B in bb-toy-inv-s3-src-ap-southeast-2-210987654321 \
         bb-toy-noinv-s3-src-ap-southeast-2-210987654321; do
  AWS_PROFILE=<aws-profile> aws s3api delete-objects --bucket "$B" \
    --delete "$(aws s3api list-object-versions --bucket "$B" \
      --output=json --query='{Objects: Versions[].{Key:Key,VersionId:VersionId}}')"
  AWS_PROFILE=<aws-profile> aws s3api delete-bucket --bucket "$B"
done

# 4. Drop the source-account role policies for the toy buckets
# (re-running source-roles setup with the toy sources removed will prune)

# 5. Post tear-down notification in operations Slack
```

## Reading the validation results

Local CLI for full picture (skips delivery; compare against what
subscribers received via Slack/email):

```bash
uv run backup health-report run
```

(`backup health-report run` has no `--config` flag at all — it always
reads from `BACKUP_CONFIG_PATH` or the default `backup-config.yaml`,
which `.env`-via-`uv run` supplies.)

What to verify per signal:

- **Class 1 ⚠ RED in the right rows.** The scenario action should map
  1:1 to the expected red note. If a scenario reds an unintended source,
  classification logic has crossed wires.
- **`toy-noinv` row always carries the "inventory disabled" `ℹ` line.**
  If it goes missing, the opt-out branch in `build_report` regressed.
- **Class-2 lines never red the row.** A source with only `ℹ` lines
  stays green at the row level.
- **Sub-line glyphs.** `⚠` for class-1/3 notes (in `notes`), `ℹ` for
  class-2 (in `info_notes`). Both render under the source row.
- **Overall headline math.** `Overall: STATUS M/N` where N includes both
  toys. M counts greens including any rotated restore-test passes.

## Related

- [ADR-009](../design/adr/ADR-009-health-check-measurement-model.md) — the signal taxonomy this runbook validates
- [Health Report user guide](../user-guide/health-report.md) — `inventory_enabled` field reference and the runtime cost breakdown
- [Operator cheatsheet](cheatsheet.md) — entry point for ops tasks; links here from "Validating classification changes"
- [Purge-from-backup runbook](purge-from-backup.md) — referenced in cleanup steps for cycle-1 scenario 1
