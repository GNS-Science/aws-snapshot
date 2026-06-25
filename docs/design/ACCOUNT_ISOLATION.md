# Account Isolation Design

## Why do it

An isolated backup account means compromised prod credentials cannot touch
backups. It is the AWS Well-Architected recommended pattern for backup
resilience — the backup account is a separate blast radius from production.

---

## Architecture shift

**Current:**
```
Prod account (210987654321)
  ├── Source S3 buckets
  ├── DynamoDB tables
  ├── Backup S3 buckets          ← same account
  ├── DynamoDB export buckets    ← same account
  └── Lambda (future)            ← same account
```

**Isolated:**
```
Prod account (210987654321)        Backup account (new or 595842668254)
  ├── Source S3 buckets      ←──────── Lambda reads across account
  ├── DynamoDB tables        ←──────── Lambda assumes role to export
  └── IAM role (assumed by         ├── Lambda
      backup Lambda)               ├── Backup S3 buckets
                                   └── DynamoDB export buckets
```

---

## What was implemented

| File | Change |
|------|--------|
| `config/models.py` | Added `source_account_role_arn: str \| None` and `source_account_id: str \| None` to `SourceConfig`; added `S3BucketConfig(arn, label)` replacing bare ARN strings; added validators ensuring account ID consistency across ARNs |
| `s3_backup.py` | `backup_source()` accepts an optional `source_session` — cross-account reads use the assumed role session, dest writes use the backup account session |
| `dynamodb_backup.py` | `export_dynamodb_table()` called via assumed source-account session. `ensure_dynamodb_backup_bucket_ready()` applies a bucket policy granting the source account IAM root (`arn:aws:iam::{source_account_id}:root`) s3:PutObject. **Note:** DynamoDB cross-account PITR exports write to S3 using the calling IAM role's credentials — not `dynamodb.amazonaws.com`. The reader role's identity policy scopes this to `bb-*` buckets in the backup account. |
| `backup_engine.py` | Assumes source-account role via `get_cross_account_session()` before S3 and DynamoDB loops; derives `source_account_id` from `SourceConfig` |
| `serverless.yml` | Lambda deployed to backup account; `sts:AssumeRole` permission in Lambda IAM role |
| `scripts/create-reader-role.py` | One-time setup script: creates `nzshm-backup-reader` role in source account with least-privilege permissions for S3 read, DynamoDB export, and s3:PutObject on `bb-*` backup buckets |

**Cross-account session helper** (`s3_backup.py`):

```python
def get_cross_account_session(session: boto3.Session, role_arn: str) -> boto3.Session:
    sts = session.client("sts")
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="nzshm-backup")["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
```

---

## New AWS infrastructure required

One-time setup in the prod account — not code changes.

1. **IAM role in prod account** — trusted by the backup account Lambda role. Needs:
   - `s3:GetObject`, `s3:ListBucket` on source buckets
   - `dynamodb:ExportTableToPointInTime`, `dynamodb:DescribeExport` on prod tables

2. **S3 bucket policies on source buckets** — allow the backup Lambda's assumed
   role to `s3:ListBucket` + `s3:GetObject`

3. **Bucket policy on DynamoDB export bucket** (backup account) — grants the
   source account IAM root `s3:PutObject`. DynamoDB cross-account PITR exports
   write using the calling IAM role's credentials, so the bucket policy must
   allow the source account IAM principal (not `dynamodb.amazonaws.com`).

---

## Cost implications

**Additional cost: essentially zero.**

| Item | Cost |
|------|------|
| S3→S3 data transfer, same region, cross-account | **Free** — AWS charges no transfer fee for same-region S3 copies regardless of account |
| DynamoDB export to cross-account S3, same region | **Free** — same rule |
| `sts:AssumeRole` calls | **Free** |
| Additional AWS account (under Organizations) | **Free** |

The per-GB storage and export costs remain identical. The $618/month estimate
is unchanged.

---

## Recommendation

The change is low-cost and low-risk to implement — the code surface is small
and well-isolated. The main investment is the one-time IAM/bucket-policy setup
in the source account, which requires source account access.

Given the savings goal is replacing a $1,700/month service, doing this properly
with account isolation is worth the engineering time.

---

## Implementation plan

Cross-account backup will be implemented and validated against **Arkivalist**
(account `816711409078`) before being applied to NSHM production (`210987654321`).

| Account | Role | Status |
|---------|------|--------|
| `595842668254` (spike/backup) | Runs Lambda, holds backup buckets | Active |
| `816711409078` (Arkivalist) | Cross-account source — restore lifecycle demo | **Implemented & verified** |
| `210987654321` (NSHM production) | Cross-account source — toshi + ths | After restore path validated |

This sequencing means the cross-account IAM pattern is proven on a lower-risk
target before any NSHM production data is involved.

---

**Status:** Implemented (cross-account S3 sync + DynamoDB PITR export verified against Arkivalist)
**Created:** 2026-03-10
**Updated:** 2026-03-16
