#!/usr/bin/env bash
# sandbox_setup.sh — Create / teardown lightweight sandbox resources mirroring
# production for testing backup run and backup schedule against real AWS.
#
# Usage:
#   scripts/sandbox_setup.sh setup      # Create resources + write backup-config.sandbox.yaml
#   scripts/sandbox_setup.sh teardown   # Delete all created resources (with confirmation)
#   scripts/sandbox_setup.sh status     # Show state of all resources
#
# Prerequisites:
#   - AWS CLI configured with sandbox account (595842668254) credentials
#   - Python venv activated (pip install -e ".[dev]")
#
# Production resource mapping:
#   PROD S3:       nzshm22-toshi-api-prod      → SANDBOX: nzshm22-toshi-api-sandbox
#   PROD S3:       ths-dataset-prod             → SANDBOX: ths-dataset-sandbox
#   PROD DynamoDB: ToshiFileObject-PROD         → SANDBOX: ToshiFileObject-PROD   (account-scoped, same name)
#   PROD DynamoDB: ToshiIdentity-PROD           → SANDBOX: ToshiIdentity-PROD
#   PROD DynamoDB: ToshiTableObject-PROD        → SANDBOX: ToshiTableObject-PROD
#   PROD DynamoDB: ToshiThingObject-PROD        → SANDBOX: ToshiThingObject-PROD
#
# Sandbox account: 595842668254   Production account: 461564345538

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"
SANDBOX_ACCOUNT="595842668254"

# S3: -prod suffix replaced with -sandbox (bucket names are global)
TOSHI_S3_BUCKET="nzshm22-toshi-api-sandbox"
THS_S3_BUCKET="ths-dataset-sandbox"

# DynamoDB: same names as prod (tables are account-scoped, no collision)
DYNAMO_TABLES=(
    "ToshiFileObject-PROD"
    "ToshiIdentity-PROD"
    "ToshiTableObject-PROD"
    "ToshiThingObject-PROD"
)

CONFIG_OUT="backup-config.sandbox.yaml"

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { echo "  [INFO]  $*"; }
ok()    { echo "  [OK]    $*"; }
warn()  { echo "  [WARN]  $*"; }
die()   { echo "  [ERROR] $*" >&2; exit 1; }

require_cmd() { command -v "$1" &>/dev/null || die "'$1' not found — install it first."; }

get_account_id() {
    aws sts get-caller-identity --query Account --output text
}

bucket_exists() {
    aws s3api head-bucket --bucket "$1" 2>/dev/null && return 0 || return 1
}

table_exists() {
    aws dynamodb describe-table --table-name "$1" --region "$REGION" \
        --query "Table.TableName" --output text 2>/dev/null | grep -q "$1"
}

assert_sandbox_account() {
    local actual
    actual=$(get_account_id)
    if [ "$actual" != "$SANDBOX_ACCOUNT" ]; then
        die "Expected sandbox account $SANDBOX_ACCOUNT but got $actual — check your AWS credentials."
    fi
}

# ── Setup ─────────────────────────────────────────────────────────────────────

cmd_setup() {
    require_cmd aws

    echo
    echo "=== Sandbox Setup ==="
    echo "Region:  $REGION"
    assert_sandbox_account
    ACCOUNT_ID=$(get_account_id)
    echo "Account: $ACCOUNT_ID (sandbox ✓)"
    echo

    # ── S3 source buckets ────────────────────────────────────────────────────
    for BUCKET in "$TOSHI_S3_BUCKET" "$THS_S3_BUCKET"; do
        if bucket_exists "$BUCKET"; then
            warn "S3 bucket already exists: $BUCKET (skipping creation)"
        else
            info "Creating S3 bucket: $BUCKET"
            aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
                --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
            aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging \
                'TagSet=[{Key=ManagedBy,Value=nzshm-backup-sandbox},{Key=Type,Value=sandbox-source},{Key=Environment,Value=sandbox}]'
            ok "Created: $BUCKET"
        fi
    done

    # ── Seed toshi S3 bucket (mirrors nzshm22-toshi-api-prod object layout) ──
    info "Seeding $TOSHI_S3_BUCKET..."
    for KEY in \
        "models/nshm22/solution-001.json" \
        "models/nshm22/solution-002.json" \
        "ruptures/nshm22/rupture-set-001.json" \
        "hazard/nshm22/hazard-curve-001.json" \
        "metadata/index.json"; do
        aws s3 cp /dev/stdin "s3://$TOSHI_S3_BUCKET/$KEY" \
            --content-type application/json >/dev/null \
            <<< "{\"source\":\"sandbox\",\"key\":\"$KEY\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
    done
    ok "Seeded $TOSHI_S3_BUCKET (5 objects)"

    # ── Seed ths S3 bucket (mirrors ths-dataset-prod layout) ─────────────────
    info "Seeding $THS_S3_BUCKET..."
    for KEY in \
        "v8/ths-nz/hazard-curves/hc-sample-001.nc" \
        "v8/ths-nz/hazard-curves/hc-sample-002.nc" \
        "v8/ths-nz/metadata/catalogue.json"; do
        aws s3 cp /dev/stdin "s3://$THS_S3_BUCKET/$KEY" \
            --content-type application/octet-stream >/dev/null \
            <<< "SANDBOX_NC_PLACEHOLDER $KEY"
    done
    ok "Seeded $THS_S3_BUCKET (3 objects)"

    # ── DynamoDB tables ───────────────────────────────────────────────────────
    for TABLE in "${DYNAMO_TABLES[@]}"; do
        if table_exists "$TABLE"; then
            warn "DynamoDB table already exists: $TABLE (skipping creation)"
        else
            info "Creating DynamoDB table: $TABLE"
            aws dynamodb create-table \
                --table-name "$TABLE" \
                --region "$REGION" \
                --attribute-definitions AttributeName=id,AttributeType=S \
                --key-schema AttributeName=id,KeyType=HASH \
                --billing-mode PAY_PER_REQUEST >/dev/null
            ok "Created: $TABLE"
        fi
    done

    # ── Wait for all tables to be ACTIVE ─────────────────────────────────────
    for TABLE in "${DYNAMO_TABLES[@]}"; do
        info "Waiting for $TABLE → ACTIVE..."
        aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
        ok "$TABLE is ACTIVE"
    done

    # ── Enable PITR ───────────────────────────────────────────────────────────
    for TABLE in "${DYNAMO_TABLES[@]}"; do
        info "Enabling PITR on $TABLE..."
        aws dynamodb update-continuous-backups \
            --table-name "$TABLE" \
            --region "$REGION" \
            --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
        ok "PITR enabled: $TABLE"
    done

    # ── Seed DynamoDB tables with realistic-shaped items ─────────────────────
    info "Seeding ToshiFileObject-PROD..."
    for i in 1 2 3; do
        aws dynamodb put-item --table-name "ToshiFileObject-PROD" --region "$REGION" \
            --item "{\"id\":{\"S\":\"FileObject-$i\"},\"file_name\":{\"S\":\"sandbox-file-$i.json\"},\"file_size\":{\"N\":\"$((i * 2048))\"},\"md5_digest\":{\"S\":\"abc$i\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded ToshiFileObject-PROD (3 items)"

    info "Seeding ToshiIdentity-PROD..."
    for i in 1 2 3; do
        aws dynamodb put-item --table-name "ToshiIdentity-PROD" --region "$REGION" \
            --item "{\"id\":{\"S\":\"Identity-$i\"},\"name\":{\"S\":\"SandboxIdentity-$i\"},\"clazz\":{\"S\":\"SandboxModel\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded ToshiIdentity-PROD (3 items)"

    info "Seeding ToshiTableObject-PROD..."
    for i in 1 2 3; do
        aws dynamodb put-item --table-name "ToshiTableObject-PROD" --region "$REGION" \
            --item "{\"id\":{\"S\":\"TableObject-$i\"},\"table_name\":{\"S\":\"SandboxTable-$i\"},\"rows\":{\"N\":\"$((i * 100))\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded ToshiTableObject-PROD (3 items)"

    info "Seeding ToshiThingObject-PROD..."
    for i in 1 2 3; do
        aws dynamodb put-item --table-name "ToshiThingObject-PROD" --region "$REGION" \
            --item "{\"id\":{\"S\":\"ThingObject-$i\"},\"clazz\":{\"S\":\"FaultSection\"},\"created\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded ToshiThingObject-PROD (3 items)"

    # ── Write backup-config.sandbox.yaml ──────────────────────────────────────
    info "Writing $CONFIG_OUT..."
    cat > "$CONFIG_OUT" <<YAML
# Sandbox backup config — generated by scripts/sandbox_setup.sh
# Mirrors production resource names in sandbox account ${ACCOUNT_ID}.
#
# Usage:
#   BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup --dry-run run --source toshi
#   BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup run --source toshi
#   BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup schedule show

general:
  region: ${REGION}
  environment: development
  lambda_arn: null

sources:
  toshi:
    display_name: "ToshiAPI (sandbox — mirrors nzshm22-toshi-api-prod)"
    s3_buckets:
      - arn:aws:s3:::${TOSHI_S3_BUCKET}
    dynamodb_tables:
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/ToshiFileObject-PROD
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/ToshiIdentity-PROD
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/ToshiTableObject-PROD
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/ToshiThingObject-PROD
    dynamodb_export_format: DYNAMODB_JSON

  ths:
    display_name: "THS_dataset_prod (sandbox — mirrors ths-dataset-prod)"
    s3_buckets:
      - arn:aws:s3:::${THS_S3_BUCKET}
    dynamodb_export_format: DYNAMODB_JSON

retention:
  hot_days: 30
  warm_days: 90
  cold_days: 365
  max_age_days: 365

restore:
  default_destination_type: temporary
  temporary_retention_days: 7
  dynamodb_always_new_table: true
  auto_approve_threshold: 100
  dual_approval_threshold: 500

notifications:
  ses:
    enabled: false
    source_email: noreply@example.com
    recipients: []

cost_tracking:
  enabled: false
  budget_alerts: false
  monthly_budget: 700
YAML
    ok "Written: $CONFIG_OUT"

    echo
    echo "=== Setup complete ==="
    echo
    echo "Source resources:"
    echo "  s3://$TOSHI_S3_BUCKET              (5 objects)"
    echo "  s3://$THS_S3_BUCKET                (3 objects)"
    echo "  DynamoDB: ToshiFileObject-PROD     (PITR, 3 items)"
    echo "  DynamoDB: ToshiIdentity-PROD       (PITR, 3 items)"
    echo "  DynamoDB: ToshiTableObject-PROD    (PITR, 3 items)"
    echo "  DynamoDB: ToshiThingObject-PROD    (PITR, 3 items)"
    echo
    echo "Backup output buckets (created on first 'backup run'):"
    echo "  s3://${TOSHI_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}"
    echo "  s3://${THS_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}"
    echo "  s3://nzshm-dynamo-backup-toshi-${REGION}-${ACCOUNT_ID}"
    echo
    echo "Demo commands:"
    echo "  export BACKUP_CONFIG_PATH=backup-config.sandbox.yaml"
    echo "  backup --dry-run run --source toshi"
    echo "  backup run --source toshi"
    echo "  backup run --source all"
    echo "  backup schedule show"
    echo "  backup schedule set --source toshi --frequency weekly --time 14:00"
    echo "  scripts/sandbox_setup.sh status"
    echo
}

# ── Status ────────────────────────────────────────────────────────────────────

cmd_status() {
    require_cmd aws
    ACCOUNT_ID=$(get_account_id)
    echo
    echo "=== Sandbox Status ==="
    echo "Account: $ACCOUNT_ID   Region: $REGION"
    echo

    echo "--- Source S3 buckets ---"
    for BUCKET in "$TOSHI_S3_BUCKET" "$THS_S3_BUCKET"; do
        if bucket_exists "$BUCKET"; then
            COUNT=$(aws s3 ls "s3://$BUCKET" --recursive 2>/dev/null | wc -l | tr -d ' ')
            echo "  ✓ s3://$BUCKET  ($COUNT objects)"
        else
            echo "  ✗ s3://$BUCKET  (NOT FOUND)"
        fi
    done

    echo
    echo "--- DynamoDB source tables ---"
    for TABLE in "${DYNAMO_TABLES[@]}"; do
        if table_exists "$TABLE"; then
            COUNT=$(aws dynamodb scan --table-name "$TABLE" --region "$REGION" \
                --select COUNT --query Count --output text 2>/dev/null || echo "?")
            PITR=$(aws dynamodb describe-continuous-backups --table-name "$TABLE" --region "$REGION" \
                --query "ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus" \
                --output text 2>/dev/null || echo "UNKNOWN")
            echo "  ✓ $TABLE  ($COUNT items, PITR=$PITR)"
        else
            echo "  ✗ $TABLE  (NOT FOUND)"
        fi
    done

    echo
    echo "--- Backup output buckets ---"
    for BUCKET in \
        "${TOSHI_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}" \
        "${THS_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}" \
        "nzshm-dynamo-backup-toshi-${REGION}-${ACCOUNT_ID}" \
        "nzshm-dynamo-backup-ths-${REGION}-${ACCOUNT_ID}"; do
        if bucket_exists "$BUCKET"; then
            COUNT=$(aws s3 ls "s3://$BUCKET" --recursive 2>/dev/null | wc -l | tr -d ' ')
            echo "  ✓ s3://$BUCKET  ($COUNT objects)"
        else
            echo "  - s3://$BUCKET  (not yet created — run 'backup run')"
        fi
    done

    echo
    echo "--- EventBridge rules ---"
    RULES=$(aws events list-rules --name-prefix "nzshm-backup-" --region "$REGION" \
        --query "Rules[].{Name:Name,State:State,Schedule:ScheduleExpression}" \
        --output text 2>/dev/null || echo "")
    if [ -z "$RULES" ]; then
        echo "  (none)"
    else
        echo "$RULES" | while IFS=$'\t' read -r name schedule state; do
            printf "  %-45s %-10s %s\n" "$name" "$state" "$schedule"
        done
    fi
    echo
}

# ── Teardown ──────────────────────────────────────────────────────────────────

cmd_teardown() {
    require_cmd aws
    assert_sandbox_account
    ACCOUNT_ID=$(get_account_id)
    echo
    echo "=== Sandbox Teardown ==="
    echo "Account: $ACCOUNT_ID (sandbox ✓)   Region: $REGION"
    echo
    read -r -p "  This will DELETE all sandbox resources. Type 'yes' to confirm: " CONFIRM
    [ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 0; }
    echo

    # Delete S3 source buckets
    for BUCKET in "$TOSHI_S3_BUCKET" "$THS_S3_BUCKET"; do
        if bucket_exists "$BUCKET"; then
            info "Emptying + deleting s3://$BUCKET..."
            aws s3 rm "s3://$BUCKET" --recursive >/dev/null 2>&1 || true
            aws s3api delete-bucket --bucket "$BUCKET" >/dev/null
            ok "Deleted: s3://$BUCKET"
        else
            warn "Not found (skipping): s3://$BUCKET"
        fi
    done

    # Delete backup output buckets
    for BUCKET in \
        "${TOSHI_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}" \
        "${THS_S3_BUCKET}-backup-${REGION}-${ACCOUNT_ID}" \
        "nzshm-dynamo-backup-toshi-${REGION}-${ACCOUNT_ID}" \
        "nzshm-dynamo-backup-ths-${REGION}-${ACCOUNT_ID}"; do
        if bucket_exists "$BUCKET"; then
            info "Emptying + deleting s3://$BUCKET..."
            aws s3api delete-bucket-lifecycle --bucket "$BUCKET" 2>/dev/null || true
            aws s3api delete-bucket-policy --bucket "$BUCKET" 2>/dev/null || true
            aws s3 rm "s3://$BUCKET" --recursive >/dev/null 2>&1 || true
            aws s3api delete-bucket --bucket "$BUCKET" >/dev/null
            ok "Deleted: s3://$BUCKET"
        else
            warn "Not found (skipping): s3://$BUCKET"
        fi
    done

    # Delete DynamoDB tables
    for TABLE in "${DYNAMO_TABLES[@]}"; do
        if table_exists "$TABLE"; then
            info "Deleting DynamoDB table: $TABLE..."
            aws dynamodb delete-table --table-name "$TABLE" --region "$REGION" >/dev/null
            ok "Deleted: $TABLE"
        else
            warn "Not found (skipping): $TABLE"
        fi
    done

    # Delete EventBridge rules
    for RULE in \
        "nzshm-backup-toshi-daily" "nzshm-backup-toshi-weekly" \
        "nzshm-backup-ths-daily"   "nzshm-backup-ths-weekly" \
        "nzshm-backup-all-daily"   "nzshm-backup-all-weekly"; do
        RULE_EXISTS=$(aws events list-rules --name-prefix "$RULE" --region "$REGION" \
            --query "Rules[?Name=='$RULE'].Name" --output text 2>/dev/null || echo "")
        if [ -n "$RULE_EXISTS" ]; then
            info "Removing targets + rule: $RULE"
            TARGET_IDS=$(aws events list-targets-by-rule --rule "$RULE" --region "$REGION" \
                --query "Targets[].Id" --output text 2>/dev/null || echo "")
            if [ -n "$TARGET_IDS" ]; then
                # shellcheck disable=SC2086
                aws events remove-targets --rule "$RULE" --region "$REGION" \
                    --ids $TARGET_IDS >/dev/null 2>/dev/null || true
            fi
            aws events delete-rule --name "$RULE" --region "$REGION" >/dev/null
            ok "Deleted rule: $RULE"
        fi
    done

    if [ -f "$CONFIG_OUT" ]; then
        rm "$CONFIG_OUT"
        ok "Removed: $CONFIG_OUT"
    fi

    echo
    echo "=== Teardown complete ==="
    echo
}

# ── Entrypoint ────────────────────────────────────────────────────────────────

case "${1:-help}" in
    setup)    cmd_setup ;;
    status)   cmd_status ;;
    teardown) cmd_teardown ;;
    *)
        echo "Usage: $0 {setup|status|teardown}"
        echo
        echo "  setup     Create sandbox resources + backup-config.sandbox.yaml"
        echo "  status    Show state of all sandbox + backup output resources"
        echo "  teardown  Delete all sandbox resources (confirms before deleting)"
        echo
        echo "Sandbox account: $SANDBOX_ACCOUNT"
        exit 1
        ;;
esac
