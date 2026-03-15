# Architecture Overview

## Account Layout

Two AWS accounts are involved. The backup Lambda runs in the **backup account** and assumes
a cross-account reader role to access source data in each **source account**.

```mermaid
graph TB
    subgraph backup["Backup Account (345678901234)"]
        EB["⏰ EventBridge\nScheduled rules"]
        L["λ Lambda\nnzshm-backup-dev"]
        SSM["🗂 SSM Parameter Store\n/nzshm-backup/dev/config"]
        S3BB["🪣 S3 Backup Buckets\nbb-{source}-s3-{label}-{region}-{acct}"]
        S3DY["🪣 DynamoDB Export Buckets\nbb-{source}-dynamo-{region}-{acct}"]
    end

    subgraph source["Source Account (e.g. Arkivalist 456789012345)"]
        ROLE["🔑 IAM Role\nnzshm-backup-reader"]
        S3SRC["🪣 Source S3 Buckets"]
        DDB["📋 DynamoDB Tables\n(PITR enabled)"]
    end

    EB -->|"triggers (scheduled / manual)"| L
    L -->|"reads config"| SSM
    L -->|"sts:AssumeRole"| ROLE
    ROLE -.->|"s3:GetObject / ListBucket"| S3SRC
    L -->|"incremental sync"| S3BB
    ROLE -.->|"dynamodb:ExportTableToPointInTime"| DDB
    DDB -->|"writes export data\n(IAM role credentials)"| S3DY
```

---

## Backup Trigger Flow

Shows the sequence from trigger to completion for a single source.

```mermaid
sequenceDiagram
    actor User
    participant EB as EventBridge
    participant L as Lambda (backup acct)
    participant SSM as SSM Parameter Store
    participant STS as STS
    participant S3src as Source S3
    participant DDB as DynamoDB
    participant S3bb as S3 Backup Bucket
    participant S3dyn as DynamoDB Export Bucket

    alt Scheduled
        EB->>L: Invoke (rate: 7 days)
    else Manual
        User->>L: aws lambda invoke
    end

    L->>SSM: GetParameter /nzshm-backup/dev/config
    SSM-->>L: Config (sources, buckets, tables)

    L->>STS: AssumeRole nzshm-backup-reader
    STS-->>L: Temporary credentials

    rect rgb(220, 235, 255)
        Note over L,S3bb: S3 Incremental Sync
        loop Each source bucket
            L->>S3src: ListObjectsV2 (via reader role)
            S3src-->>L: Objects + ETags
            L->>S3bb: CopyObject (changed objects only)
        end
    end

    rect rgb(220, 255, 230)
        Note over L,S3dyn: DynamoDB PITR Export
        L->>S3dyn: ensure bucket exists + apply IAM root bucket policy
        loop Each DynamoDB table
            L->>DDB: ExportTableToPointInTime (via reader role)
            DDB-->>L: ExportArn (async — export runs in background)
        end
        Note over DDB,S3dyn: DynamoDB writes parquet/JSON export<br/>using reader role IAM credentials
    end
```

---

## Bucket Naming Convention

| Type | Pattern | Example |
|------|---------|---------|
| S3 backup | `bb-{source}-s3-{label}-{region}-{source-acct}` | `bb-arkivalist-s3-deploy-ap-southeast-2-456789012345` |
| DynamoDB export | `bb-{source}-dynamo-{region}-{source-acct}` | `bb-arkivalist-dynamo-ap-southeast-2-456789012345` |

All backup buckets are:
- Tagged `ManagedBy: nzshm-backup`
- Protected against deletion (no `s3:DeleteObject` in Lambda IAM role)
- Tiered: Standard (30d) → Glacier Instant (90d) → Deep Archive (365d)

---

## Cross-Account IAM

For each cross-account source, a one-time setup creates a reader role:

```
scripts/create-reader-role.py --backup-account-id 345678901234 \
    --dynamodb-tables table1 table2 \
    --s3-buckets bucket1
```

The reader role grants:
- `s3:GetObject`, `s3:ListBucket` on named source buckets
- `dynamodb:ExportTableToPointInTime`, `dynamodb:DescribeContinuousBackups` on named tables
- `dynamodb:ListExports`, `dynamodb:DescribeExport` for status queries
- `s3:PutObject` on `bb-*` backup buckets (needed because DynamoDB cross-account exports
  write using the calling role's credentials, not the `dynamodb.amazonaws.com` service principal)
