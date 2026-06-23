# Open-Source Migration Plan

## Goal

Publish `nzshm-backup` to PyPI as a generic AWS incremental backup CLI usable by any
team running BLOB-heavy workloads on AWS S3 + DynamoDB. The core engine is already
generic — the work is packaging, naming, and removing GNS/NSHM-specific artefacts.

---

## Why it's worth doing

The tool solves a real problem beyond NSHM:

- AWS Backup is expensive for large, largely-static S3 corpora (vault pricing, no lifecycle tiers)
- The incremental ETag sync + S3 Lifecycle pattern cuts costs by 97%+ for write-once BLOBs
- Cross-account backup isolation (source ≠ backup account) is a well-known pattern with no
  good off-the-shelf CLI tool
- The S3 Batch + DynamoDB PITR combination is not well documented as a unified solution

Pitch: *"Replace AWS Backup at 97% lower cost with a CLI that takes 30 minutes to set up."*

---

## Required changes

### 1. Package rename

`nzshm-backup` → something generic. Candidates:

| Name | Notes |
|------|-------|
| `aws-vault-backup` | Clear, but "vault" has Hashicorp connotations |
| `s3-glacier-backup` | Describes the mechanism |
| `aws-incremental-backup` | Accurate, descriptive |
| `glacierback` | Short, memorable |

### 2. Replace Serverless Framework with boto3 deploy script

Serverless Framework v4 requires a mandatory account login with the Serverless organisation —
a barrier for open-source users. Options considered:

| Option | Verdict |
|--------|---------|
| Serverless v3 (downgrade) | Still an external tool (`npm` dependency) |
| AWS CDK | Requires `cdk bootstrap` stack in target account — same overhead as Serverless |
| **boto3 deploy script** | **Preferred — zero extra tools, pure Python + AWS credentials** |

**Plan:** replace `serverless.yml` with `scripts/deploy.py` (boto3). The script:
- Zips the package (equivalent to Serverless packaging)
- Creates or updates the Lambda function
- Attaches the execution IAM role
- Sets environment variables
- Registers EventBridge permissions

Users deploy with:
```bash
uv run python scripts/deploy.py --stage prod --region ap-southeast-2
```

~150 lines of boto3, fully readable, no mystery stacks, no external accounts.

### 3. Remove GNS/NSHM-specific content

Destinations follow the shim strategy in section 3a below.

| File / content | Action |
|---------------|--------|
| `backup-config.production.yaml` | Move to `nzshm-backup-ops` (private); keep `backup-config.example.yaml` here as documented template |
| `docs/PROD-DEPLOY-LOG.md` | Move to `nzshm-backup-ops` (private) |
| Account IDs (`123456789012`, `210987654321`) | Replace with placeholders in remaining public docs; real values live only in `nzshm-backup-ops` |
| Source names (`toshi`, `ths`, `weka`, `static`) | Replace with generic examples (`myapp`, `mydata`) in public docs; concrete names remain in `nzshm-backup-ops` runbooks |
| Operator identities (emails, SSO start URL `example.awsapps.com`, `<aws-profile>` profile) | Remove from public docs entirely; live only in `nzshm-backup-ops` |
| Serverless org `gnssciencenshm` | Removed with `serverless.yml` replacement (section 2) |
| `docs/SESSION_SUMMARY_REPORT.md`, `docs/development/UPDATE_REPORT_2026-03-30.md` | Move to `nzshm-backup-ops` (private) — internal session records, no public value |
| GNS-specific operator runbooks (parts of `docs/operations/cheatsheet.md`, `enabling-notifications.md`, `inventory-bucket-recovery.md`) | Split: generic patterns stay in public docs; site-specific commands move to `nzshm-backup-ops` |

Concrete footprint (snapshot 2026-06-23): 168 account-ID hits, 13 operator-identity hits, 151 GNS/NSHM brand strings, 133 concrete bucket/dataset names across the repo. 21 of ~30 `docs/` files contain at least one — i.e. nearly all of them.

### 3a. Where the GNS-specific content lives — shim repo strategy

The "Action" column above is partly "remove" and partly "move to a private location". Two repo patterns considered for the private location:

**Forked private repo** — fork the public repo into a private `gns-science/nzshm-backup-internal`, carry GNS-only files on top, periodically pull upstream. Rejected because:

- **Constant merge-conflict tax.** Every upstream change to `serverless.yml`, ADRs, config example, or any docs file we've also edited locally produces a conflict at pull time. Six months in, "I'll skip this pull" becomes the easy default and the fork drifts.
- **Mixes secrets with code.** Operational state (PROD-DEPLOY-LOG with timestamps, operator names) sits in the same tree as engine code. A single mis-clicked repo-visibility toggle leaks everything.
- **Discourages upstream contribution.** Any GNS-specific quirk tends to stay in the fork because the upstream round-trip feels slower than fixing in place — defeating the OSS positioning we just invested in.
- **Larger permissions blast radius.** Read access to the fork = read access to engine + ops state.

**Private shim repo** (chosen) — public OSS package consumed as a dependency, private `gns-science/nzshm-backup-ops` carries only operational state:

```
gns-science/nzshm-backup-ops/        (PRIVATE)
├── README.md                         # GNS-specific quickstart, links to upstream docs
├── pyproject.toml                    # declares aws-incremental-backup>=X.Y.Z
├── backup-config.production.yaml
├── deploy/serverless.yml             # GNS account IDs, real IAM role names
├── docs/
│   ├── PROD-DEPLOY-LOG.md
│   ├── cheatsheet.md
│   ├── enabling-notifications.md
│   └── inventory-bucket-recovery.md
└── Makefile                          # convenience targets calling `backup …`
```

Why this wins:

- **Clean separation.** Engine = public/generic. Operational state = private/GNS. Permissions match the actual sensitivity.
- **Upgrades are one-line.** Bump pin in `pyproject.toml` from `1.4.0` → `1.5.0`. No merge conflicts ever.
- **Forces good design upstream.** When the shim needs a site-specific hook, the abstraction has to land in the OSS API — which makes the OSS project better.
- **Audit-trail clarity.** "Did the engine change or did config change?" is answered by checking which repo's git log moved.
- **Dependabot on the shim** monitors upstream releases and proposes version bumps automatically.
- **Survives a second OSS adopter.** GNS becomes one consumer among others; the OSS API hardens through external pressure rather than drifting around GNS-specific assumptions.

Costs accepted:

- Two repos to clone and onboard. Mitigated by a short README in the shim pointing at upstream.
- Hotfixes can require a temporary git-ref pin if upstream hasn't released yet (`aws-incremental-backup @ git+https://…@hotfix-abc`). Escape hatch exists in one line of `pyproject.toml`.
- Repo-level customisation must go through the OSS package's config surface, not by editing the OSS code in place. This is a feature, but does require thinking about the interface.

**Working rule for the boundary.** Anything that is a *behaviour change* goes upstream to the OSS package. The shim only carries *configuration* and *operational state*. That single rule is what prevents the two repos from drifting apart.

### 4. Generalise documentation

- `docs/getting-started/` — rewrite quickstart around a generic two-source example
- `docs/design/backup-solution-plan.md` — replace NSHM data volumes with generic placeholders
- `docs/architecture/cost-model.md` — keep cost model but use generic source names
- `README.md` — write a proper open-source README with badges, install instructions, 5-minute quickstart

### 5. Config example

Replace production config references with a generic `backup-config.example.yaml` committed
to the repo, covering:
- Same-account source (simple case)
- Cross-account source with IAM roles
- S3 Batch for large buckets
- DynamoDB PITR export

### 6. PyPI packaging

`pyproject.toml` already uses `uv` and src-layout — minimal changes needed:

- Set `name` to chosen package name
- Add `description`, `keywords`, `classifiers`
- Add `[project.urls]` (homepage, repository, documentation)
- Confirm `license` field
- Publish: `uv build && uv publish`

---

## What stays the same

- All core Python modules (`s3_backup.py`, `dynamodb_backup.py`, `backup_engine.py`, etc.)
- Pydantic config schema (`config/models.py`) — already generic
- All CLI commands (`run`, `check`, `schedule`, `restore`, `config`, `status`, `test`)
- IAM role scripts (`create-backup-roles.py`, `create-source-roles.py`)
- Test suite

---

## Suggested sequencing

1. **Choose package name.**
2. **Stand up `nzshm-backup-ops` (private)** as the shim destination — empty repo + README + `pyproject.toml` placeholder. Doesn't depend on anything else; first concrete step.
3. Write `scripts/deploy.py` (boto3) + verify it deploys cleanly.
4. Remove `serverless.yml` (or keep as legacy/optional alongside deploy script).
5. **Scrub NSHM-specific content from docs and config**, moving the GNS-specific pieces into `nzshm-backup-ops` per the destination table in section 3.
6. Write README.
7. Publish to PyPI (test index first).
8. **Cutover.** Only after `nzshm-backup-ops` is verified working against the published PyPI package: delete the GNS-specific files from the public repo. Until cutover, both repos hold copies — keeps a working escape route if the shim setup hits an unforeseen issue.
9. Decide on doc publishing (issue #46). Only safe to publish public docs once steps 5 and 8 are complete.

**Cutover safety rule:** never delete a file from the public repo until its replacement in `nzshm-backup-ops` has been used in at least one real deploy.

---

**Created:** 2026-04-15
**Amended:** 2026-06-23 — shim-repo strategy (section 3a), destination table in section 3, sequencing reflects cutover safety
**Status:** Draft — work beginning under issue tracked separately
