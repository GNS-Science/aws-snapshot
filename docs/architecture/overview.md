# Architecture Overview

## Account Layout

Two AWS accounts are involved. The backup Lambda runs in the **backup account** and assumes
a cross-account role to access source data in each **source account**.

Two cross-account roles exist per source (created by `scripts/create-source-roles.py`):

- **`nzshm-backup-reader`** — read-only; assumed by the backup Lambda for S3 sync and DynamoDB exports
- **`nzshm-backup-restore`** — assumed by the restore CLI and pitr-watcher Lambda for PITR restore, PITR re-enable, and tagging

```mermaid
graph TB
    subgraph backup["Backup Account (595842668254)"]
        EB["⏰ EventBridge\nScheduled rules"]
        L["λ Lambda\nnzshm-backup-dev"]
        PW["λ pitr-watcher\n(rate: 5 min, disabled when idle)"]
        SSM["🗂 SSM Parameter Store\n/nzshm-backup/dev/config\n/nzshm-backup/pending-restores"]
        S3BB["🪣 S3 Backup Buckets\nbb-{source}-s3-{label}-{region}-{acct}"]
        S3DY["🪣 DynamoDB Export Buckets\nbb-{source}-dynamo-{region}-{acct}"]
    end

    subgraph source["Source Account (e.g. Arkivalist 816711409078)"]
        READER["🔑 IAM Role\nnzshm-backup-reader"]
        RESTORE["🔑 IAM Role\nnzshm-backup-restore"]
        S3SRC["🪣 Source S3 Buckets"]
        DDB["📋 DynamoDB Tables\n(PITR enabled)"]
        DDB2["📋 Restored DynamoDB Tables\n(<name>-restore)"]
    end

    EB -->|"triggers (scheduled / manual)"| L
    L -->|"reads config"| SSM
    L -->|"sts:AssumeRole"| READER
    READER -.->|"s3:GetObject / ListBucket"| S3SRC
    L -->|"incremental sync"| S3BB
    READER -.->|"dynamodb:ExportTableToPointInTime"| DDB
    DDB -->|"writes export data\n(IAM role credentials)"| S3DY

    PW -->|"reads/writes pending list"| SSM
    PW -->|"sts:AssumeRole"| RESTORE
    RESTORE -.->|"dynamodb:DescribeTable\ndynamodb:UpdateContinuousBackups\ndynamodb:TagResource"| DDB2
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

## Restore Flow

DynamoDB restores are submit-and-return (async, 2–8 hours). S3 restores use
direct copy (small buckets) or S3 Batch Operations (large buckets).

```mermaid
sequenceDiagram
    actor User
    participant CLI as backup restore run (CLI)
    participant STS as STS
    participant DDB as DynamoDB (source acct)
    participant SSM as SSM Parameter Store
    participant EB as EventBridge
    participant PW as pitr-watcher Lambda
    participant DDB2 as Restored Table (source acct)

    User->>CLI: backup restore run --source X --to-point-in-time T

    CLI->>STS: AssumeRole nzshm-backup-restore
    STS-->>CLI: Temporary credentials

    loop Each DynamoDB table
        CLI->>DDB: RestoreTableToPointInTime → <name>-restore
        DDB-->>CLI: RestoreArn (table is CREATING)
        CLI->>SSM: PutParameter — append {restore_arn, source, source_table_arn, restore_point}
    end

    CLI->>EB: EnableRule nzshm-backup-pitr-watcher

    Note over DDB2: 2–8 hours later...

    loop Every 5 minutes
        EB->>PW: Invoke
        PW->>SSM: GetParameter — read pending list
        PW->>STS: AssumeRole nzshm-backup-restore
        PW->>DDB2: DescribeTable
        alt Table ACTIVE
            PW->>DDB2: UpdateContinuousBackups (enable PITR)
            PW->>DDB2: TagResource (RestoredBy, RestoredFrom, RestoredAt)
            PW->>SSM: PutParameter — remove entry from list
        else Table still CREATING
            Note over PW: retry next invocation
        end
        alt No pending entries remain
            PW->>EB: DisableRule nzshm-backup-pitr-watcher
        end
    end
```

---

## Bucket Naming Convention

| Type | Pattern | Example |
|------|---------|---------|
| S3 backup | `bb-{source}-s3-{label}-{region}-{source-acct}` | `bb-arkivalist-s3-deploy-ap-southeast-2-816711409078` |
| DynamoDB export | `bb-{source}-dynamo-{region}-{source-acct}` | `bb-arkivalist-dynamo-ap-southeast-2-816711409078` |

All backup buckets are:
- Tagged `ManagedBy: nzshm-backup`
- Protected against deletion (no `s3:DeleteObject` in Lambda IAM role)
- Tiered: Standard (30d) → Glacier Instant (90d) → Deep Archive (365d)

---

## Cross-Account IAM

For each cross-account source, a one-time setup creates both roles:

```bash
scripts/create-source-roles.py \
    --config backup-config.yaml \
    --source <alias>
```

Both role ARNs are written back to the config file automatically.

**`nzshm-backup-reader`** (assumed by backup Lambda):
- `s3:GetObject`, `s3:ListBucket` on named source buckets
- `dynamodb:ExportTableToPointInTime`, `dynamodb:DescribeContinuousBackups` on named tables
- `dynamodb:ListExports`, `dynamodb:DescribeExport` for status queries
- `s3:PutObject` on `bb-*` backup buckets (DynamoDB cross-account exports write
  using the calling role's credentials, not the `dynamodb.amazonaws.com` service principal)

**`nzshm-backup-restore`** (assumed by restore CLI and pitr-watcher Lambda):
- `dynamodb:RestoreTableToPointInTime` on named source tables
- `dynamodb:*` on `table/*` in the source account — required because
  `RestoreTableToPointInTime` makes undocumented internal calls (Scan, Query, etc.)
  on the restore target table; resource-level scoping is not practical
