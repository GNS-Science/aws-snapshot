# Architecture Overview

## Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **backup CLI** | Backup account — local or Lambda | Runs incremental S3 sync and DynamoDB PITR exports; all subcommands |
| **EventBridge rules** | Backup account | Triggers the backup Lambda on a schedule (`nzshm-backup-{source}-{frequency}`) |
| **backup Lambda** | Backup account | Executes `backup run` on a schedule; same code as the CLI |
| **pitr-watcher Lambda** | Backup account | Polls every 5 min for completed DynamoDB restores; re-enables PITR and applies tags |
| **SSM Parameter Store** | Backup account | Stores config (`/nzshm-backup/dev/config`) and pending restore list (`/nzshm-backup/pending-restores`) |
| **S3 backup buckets** | Backup account | Receive incremental S3 copies; tiered Standard (0–30d) → Glacier Instant Retrieval (forever) — see [ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md) |
| **DynamoDB export buckets** | Backup account | Receive `ExportTableToPointInTime` parquet/JSON snapshots |
| **`nzshm-backup-reader` IAM role** | Source account | Assumed by backup Lambda; read-only S3 + DynamoDB export access |
| **`nzshm-backup-restore` IAM role** | Source account | Assumed by restore CLI and pitr-watcher; DynamoDB PITR restore + PITR re-enable + tagging |

Two AWS accounts are involved. The backup Lambda runs in the **backup account** and assumes
a cross-account role to access source data in each **source account**. See
[Account Isolation](../design/ACCOUNT_ISOLATION.md) and
[IAM Security Decisions](../design/iam-security-decisions.md) for full IAM details.

---

## Backup

```mermaid
graph TB
    subgraph backup["Backup Account (595842668254)"]
        EB["⏰ EventBridge\nScheduled rules"]
        L["λ backup Lambda\nnzshm-backup-dev"]
        SSM["🗂 SSM\n/nzshm-backup/dev/config"]
        S3BB["🪣 S3 Backup Buckets\nbb-{source}-s3-{label}-…"]
        S3DY["🪣 DynamoDB Export Buckets\nbb-{source}-dynamo-…"]
    end

    subgraph source["Source Account (e.g. 816711409078)"]
        READER["🔑 nzshm-backup-reader"]
        S3SRC["🪣 Source S3 Buckets"]
        DDB["📋 DynamoDB Tables\n(PITR enabled)"]
    end

    EB -->|"triggers"| L
    L -->|"reads config"| SSM
    L -->|"sts:AssumeRole"| READER
    READER -.->|"s3:GetObject / ListBucket"| S3SRC
    L -->|"incremental ETag sync"| S3BB
    READER -.->|"ExportTableToPointInTime"| DDB
    DDB -->|"writes export\n(reader role creds)"| S3DY
```

### Backup sequence

EventBridge (or a manual CLI call) triggers the Lambda, which reads config from SSM,
assumes the reader role in the source account, and runs an incremental S3 sync followed
by DynamoDB PITR exports.

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
        User->>L: backup run --source X
    end

    L->>SSM: GetParameter — config
    SSM-->>L: sources, buckets, tables

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
        L->>S3dyn: ensure bucket exists + apply bucket policy
        loop Each DynamoDB table
            L->>DDB: ExportTableToPointInTime (via reader role)
            DDB-->>L: ExportArn (async — export runs in background)
        end
        Note over DDB,S3dyn: DynamoDB writes parquet/JSON<br/>using reader role credentials
    end
```

---

## Restore

```mermaid
graph TB
    subgraph backup["Backup Account (595842668254)"]
        CLI["💻 backup restore run\n(CLI or Lambda)"]
        PW["λ pitr-watcher\n(rate: 5 min, idle when no restores)"]
        SSM2["🗂 SSM\n/nzshm-backup/pending-restores"]
        EB2["⏰ EventBridge\nnzshm-backup-pitr-watcher"]
    end

    subgraph source["Source Account (e.g. 816711409078)"]
        RESTORE["🔑 nzshm-backup-restore"]
        DDB["📋 Source DynamoDB Tables"]
        DDB2["📋 Restored Tables\n(<name>-restore)"]
    end

    CLI -->|"sts:AssumeRole"| RESTORE
    RESTORE -.->|"RestoreTableToPointInTime"| DDB
    DDB -->|"creates"| DDB2
    CLI -->|"writes pending entry"| SSM2
    CLI -->|"EnableRule"| EB2
    EB2 -->|"triggers every 5 min"| PW
    PW -->|"reads/clears pending list"| SSM2
    PW -->|"sts:AssumeRole"| RESTORE
    RESTORE -.->|"enable PITR + TagResource\n(when table ACTIVE)"| DDB2
    PW -->|"DisableRule (list empty)"| EB2
```

### Restore sequence

DynamoDB restores are submit-and-return (async, 2–8 hours to complete). The
pitr-watcher Lambda polls SSM every 5 minutes to detect when the restored table
becomes ACTIVE, then re-enables PITR and applies tags.

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
        CLI->>SSM: PutParameter — append pending restore entry
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

All backup buckets are tagged `ManagedBy: nzshm-backup`, protected against deletion
(no `s3:DeleteObject` in Lambda IAM), and tiered Standard (0–30d) → Glacier Instant
Retrieval (30d+, forever — see [ADR-006](../design/adr/ADR-006-simplify-storage-tiers-drop-deep-archive.md)).

---

## Cross-Account IAM Setup

One-time setup per source account:

```bash
python scripts/create-source-roles.py --config backup-config.yaml --source <alias>
```

Both role ARNs are written back to the config automatically. See
[IAM Security Decisions](../design/iam-security-decisions.md) for the full permission breakdown.
