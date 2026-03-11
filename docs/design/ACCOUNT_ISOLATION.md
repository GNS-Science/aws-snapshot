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
Prod account (210987654321)        Backup account (new or 345678901234)
  ├── Source S3 buckets      ←──────── Lambda reads across account
  ├── DynamoDB tables        ←──────── Lambda assumes role to export
  └── IAM role (assumed by         ├── Lambda
      backup Lambda)               ├── Backup S3 buckets
                                   └── DynamoDB export buckets
```

---

## Code changes required

Small surface area — roughly 1 day of work.

| File | Change |
|------|--------|
| `config/models.py` | Add `prod_account_role_arn: str \| None` to `GeneralConfig` |
| `s3_backup.py` | `sync_bucket()` needs two clients — one for source (assumed role), one for dest (backup account). Currently uses one client for both. |
| `dynamodb_backup.py` | `export_dynamodb_table()` must be called via assumed prod-account role. `ensure_dynamodb_backup_bucket_ready()` needs to add a bucket policy allowing `dynamodb.amazonaws.com` as a service principal to write cross-account. |
| `run_backup.py` + `lambda_handler.py` | Add `assume_role()` call to get prod-account session before the S3 and DynamoDB loops |
| `serverless.yml` | Deploy Lambda to backup account; add `sts:AssumeRole` IAM permission |

**New helper needed** (5–10 lines):

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

3. **Bucket policy on DynamoDB export bucket** (backup account) — allow
   `dynamodb.amazonaws.com` service principal to `s3:PutObject` (required for
   cross-account PITR export destination)

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
(account `456789012345`) before being applied to NSHM production (`210987654321`).

| Account | Role | Status |
|---------|------|--------|
| `345678901234` (spike/backup) | Runs Lambda, holds backup buckets | Active |
| `456789012345` (Arkivalist) | Cross-account source — restore lifecycle demo | **Next target** |
| `210987654321` (NSHM production) | Cross-account source — toshi + ths | After Arkivalist validated |

This sequencing means the cross-account IAM pattern is proven on a lower-risk
target before any NSHM production data is involved.

---

**Status:** Not yet implemented
**Created:** 2026-03-10
**Updated:** 2026-03-12
