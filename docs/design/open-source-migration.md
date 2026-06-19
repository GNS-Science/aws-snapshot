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

| File / content | Action |
|---------------|--------|
| `backup-config.production.yaml` | Remove from repo (add to `.gitignore`); keep as documented example only |
| `docs/PROD-DEPLOY-LOG.md` | Remove (internal operational log) |
| Account IDs (`123456789012`, `210987654321`) | Replace with placeholders in all docs |
| Source names (`toshi`, `ths`, `weka`, `static`) | Replace with generic examples (`myapp`, `mydata`) |
| Serverless org `gnssciencenshm` | Removed with `serverless.yml` replacement |
| `docs/SESSION_SUMMARY_REPORT.md`, `docs/development/UPDATE_REPORT_2026-03-30.md` | Remove (internal) |

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

1. Choose package name
2. Write `scripts/deploy.py` (boto3) + verify it deploys cleanly
3. Remove `serverless.yml` (or keep as legacy/optional alongside deploy script)
4. Scrub NSHM-specific content from docs and config
5. Write README
6. Publish to PyPI (test index first)

---

**Created:** 2026-04-15
**Status:** Draft — not yet started
