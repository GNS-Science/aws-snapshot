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

### 2. Replace Serverless Framework with AWS SAM template

Serverless Framework v4 requires a mandatory account login with the Serverless organisation —
a barrier for open-source users. Options considered:

| Option | Verdict |
|--------|---------|
| Serverless Framework v3 (downgrade) | Rejected: `npm` dependency + abandonware risk. |
| Serverless Framework v4 (status quo) | Rejected: forced Serverless-org account (the migration trigger). |
| AWS CDK | Rejected: requires `cdk bootstrap` stack in target account — same overhead as Serverless. |
| boto3 deploy script | Rejected: ~300-500 lines of orchestration code the OSS maintainer carries forever. Eventual consistency, stuck rollbacks, and concurrent-deploy edge cases all become our problem. Hard to test cleanly (requires mocking CFN/S3/Lambda APIs). |
| **AWS SAM template (`sam.yaml`)** | **Preferred — first-party AWS tool, no bootstrap stack, no subscription, declaration not orchestration.** |

**Why SAM specifically.** SAM is a thin CloudFormation transform — `sam deploy` renders the template to CFN and calls `aws cloudformation deploy`. It inherits all of CloudFormation's atomic-update + rollback behaviour without the maintenance load of writing that logic ourselves. Unlike CDK, no special stack needs to exist in the target account before first deploy. Unlike Serverless Framework, the tool is AWS-first-party and can't pivot to a SaaS model (which is exactly the failure mode that prompted this migration).

The "external tool" cost is real but small:
- Install: `pip install aws-sam-cli` (native to the Python audience this package targets).
- Docker for `sam build --use-container` — same requirement as Serverless Framework today.

**Plan:** replace `serverless.yml` with `sam.yaml` (SAM) at the package root, plus a thin `samconfig.toml` for default deploy parameters. The template mirrors today's serverless.yml resources almost line-for-line (Lambda function, log group, IAM role, EventBridge rules, SNS topic, alarms, subscriptions). The translation is mechanical — declaration syntax differs, but the resource shape is identical.

Users deploy with:
```bash
sam build --use-container
sam deploy --stage prod --region ap-southeast-2
```

Once it works, `serverless.yml` is removed in the same PR.

#### Why not "reference templates only" (option C, for the record)

A third option considered was: the OSS package ships only the engine + CLI, and the deploy story lives entirely in user-supplied tooling (with reference templates in `examples/`). Rejected for now because (a) "batteries included" lowers first-impression friction for new adopters, and (b) GNS itself benefits from a canonical template to fork into `nzshm-backup-ops` rather than from a blank slate. We can revisit if the canonical template ever feels like it's holding back adopters who want CDK / Terraform / Pulumi flows.

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
3. Write `sam.yaml` (SAM) + `samconfig.toml` and verify `sam deploy` produces a working stack equivalent to the current `serverless.yml` deploy.
4. Remove `serverless.yml` once SAM parity is confirmed.
5. **Scrub NSHM-specific content from docs and config**, moving the GNS-specific pieces into `nzshm-backup-ops` per the destination table in section 3.
6. Write README.
7. Publish to PyPI (test index first).
8. **Cutover.** Only after `nzshm-backup-ops` is verified working against the published PyPI package: delete the GNS-specific files from the public repo. Until cutover, both repos hold copies — keeps a working escape route if the shim setup hits an unforeseen issue.
9. Decide on doc publishing (issue #46). Only safe to publish public docs once steps 5 and 8 are complete.

**Cutover safety rule:** never delete a file from the public repo until its replacement in `nzshm-backup-ops` has been used in at least one real deploy.

---

**Created:** 2026-04-15
**Amended:** 2026-06-23 — shim-repo strategy (section 3a), destination table in section 3, sequencing reflects cutover safety, deploy story moved from boto3 script to AWS SAM template (section 2)
**Status:** Draft — work beginning under issue tracked separately
