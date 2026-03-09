#!/usr/bin/env bash
# sandbox_setup.sh — Create / teardown lightweight sandbox resources for testing
# backup run and backup schedule against real AWS without touching production.
#
# Usage:
#   scripts/sandbox_setup.sh setup      # Create resources + write backup-config.sandbox.yaml
#   scripts/sandbox_setup.sh teardown   # Delete all created resources
#   scripts/sandbox_setup.sh status     # Show resource state
#
# Prerequisites:
#   - AWS CLI configured with sandbox account credentials
#   - Python venv activated (for 'backup' command in post-setup check)
#   - jq installed (brew install jq)
#
# Resources created (all names include "sandbox" so teardown is safe):
#   S3:       nzshm-sandbox-toshi-source
#             nzshm-sandbox-ths-source
#   DynamoDB: nzshm-sandbox-filetable
#             nzshm-sandbox-thingtable
#
# These are small, seeded resources — no large data, no production content.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"
TOSHI_S3_BUCKET="nzshm-sandbox-toshi-source"
THS_S3_BUCKET="nzshm-sandbox-ths-source"
DYNAMO_FILE_TABLE="nzshm-sandbox-filetable"
DYNAMO_THING_TABLE="nzshm-sandbox-thingtable"
CONFIG_OUT="backup-config.sandbox.yaml"

# ── Helpers ──────────────────────────────────────────────────────────────────

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

# ── Setup ─────────────────────────────────────────────────────────────────────

cmd_setup() {
    require_cmd aws
    require_cmd jq

    echo
    echo "=== Sandbox Setup ==="
    echo "Region:  $REGION"
    ACCOUNT_ID=$(get_account_id)
    echo "Account: $ACCOUNT_ID"
    echo

    # ── S3 source buckets ───────────────────────────────────────────────────
    for BUCKET in "$TOSHI_S3_BUCKET" "$THS_S3_BUCKET"; do
        if bucket_exists "$BUCKET"; then
            warn "S3 bucket already exists: $BUCKET (skipping)"
        else
            info "Creating S3 bucket: $BUCKET"
            if [ "$REGION" = "us-east-1" ]; then
                aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
            else
                aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
                    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
            fi
            aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging \
                'TagSet=[{Key=ManagedBy,Value=nzshm-backup-sandbox},{Key=Type,Value=sandbox-source}]'
            ok "Created: $BUCKET"
        fi
    done

    # ── Seed S3 buckets with sample objects ────────────────────────────────
    info "Seeding $TOSHI_S3_BUCKET with sample objects..."
    for KEY in models/sample-model-001.json models/sample-model-002.json \
               ruptures/sample-rupture.json metadata/index.json; do
        aws s3 cp /dev/stdin "s3://$TOSHI_S3_BUCKET/$KEY" \
            --content-type application/json >/dev/null \
            <<< "{\"source\":\"sandbox\",\"key\":\"$KEY\",\"created\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
    done
    ok "Seeded $TOSHI_S3_BUCKET (4 objects)"

    info "Seeding $THS_S3_BUCKET with sample objects..."
    for KEY in datasets/sample-dataset-001.nc datasets/sample-dataset-002.nc \
               metadata/catalogue.json; do
        aws s3 cp /dev/stdin "s3://$THS_S3_BUCKET/$KEY" \
            --content-type application/octet-stream >/dev/null \
            <<< "SANDBOX_PLACEHOLDER_$KEY"
    done
    ok "Seeded $THS_S3_BUCKET (3 objects)"

    # ── DynamoDB tables ─────────────────────────────────────────────────────
    for TABLE in "$DYNAMO_FILE_TABLE" "$DYNAMO_THING_TABLE"; do
        if table_exists "$TABLE"; then
            warn "DynamoDB table already exists: $TABLE (skipping)"
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

    # ── Wait for tables to be ACTIVE ───────────────────────────────────────
    for TABLE in "$DYNAMO_FILE_TABLE" "$DYNAMO_THING_TABLE"; do
        info "Waiting for $TABLE to become ACTIVE..."
        aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
        ok "$TABLE is ACTIVE"
    done

    # ── Enable PITR on DynamoDB tables ─────────────────────────────────────
    for TABLE in "$DYNAMO_FILE_TABLE" "$DYNAMO_THING_TABLE"; do
        info "Enabling Point-in-Time Recovery on $TABLE..."
        aws dynamodb update-continuous-backups \
            --table-name "$TABLE" \
            --region "$REGION" \
            --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
        ok "PITR enabled: $TABLE"
    done

    # ── Seed DynamoDB tables ────────────────────────────────────────────────
    info "Seeding $DYNAMO_FILE_TABLE..."
    for i in 1 2 3; do
        aws dynamodb put-item \
            --table-name "$DYNAMO_FILE_TABLE" \
            --region "$REGION" \
            --item "{\"id\":{\"S\":\"file-$i\"},\"name\":{\"S\":\"SandboxFile-$i\"},\"size\":{\"N\":\"$((i * 1024))\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded $DYNAMO_FILE_TABLE (3 items)"

    info "Seeding $DYNAMO_THING_TABLE..."
    for i in 1 2 3; do
        aws dynamodb put-item \
            --table-name "$DYNAMO_THING_TABLE" \
            --region "$REGION" \
            --item "{\"id\":{\"S\":\"thing-$i\"},\"name\":{\"S\":\"SandboxThing-$i\"},\"type\":{\"S\":\"FaultSection\"},\"source\":{\"S\":\"sandbox\"}}" >/dev/null
    done
    ok "Seeded $DYNAMO_THING_TABLE (3 items)"

    # ── Write backup-config.sandbox.yaml ───────────────────────────────────
    info "Writing $CONFIG_OUT..."
    cat > "$CONFIG_OUT" <<YAML
# Sandbox backup config — generated by scripts/sandbox_setup.sh
# Use with: BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup run --source toshi

general:
  region: ${REGION}
  environment: development
  lambda_arn: null

sources:
  toshi:
    display_name: "ToshiAPI (sandbox)"
    s3_buckets:
      - arn:aws:s3:::${TOSHI_S3_BUCKET}
    dynamodb_tables:
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DYNAMO_FILE_TABLE}
      - arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DYNAMO_THING_TABLE}
    dynamodb_export_format: DYNAMODB_JSON

  ths:
    display_name: "THS_dataset_prod (sandbox)"
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
    echo "Next steps:"
    echo "  1. Activate your venv:  source .venv/bin/activate"
    echo "  2. Dry-run test:        BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup --dry-run run --source toshi"
    echo "  3. Live run:            BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup run --source toshi"
    echo "  4. Check schedules:     BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup schedule show"
    echo "  5. Set a schedule:      BACKUP_CONFIG_PATH=backup-config.sandbox.yaml backup schedule set --source toshi --frequency weekly --time 14:00"
    echo
    echo "Resources created:"
    echo "  S3:       s3://$TOSHI_S3_BUCKET  (4 objects)"
    echo "  S3:       s3://$THS_S3_BUCKET  (3 objects)"
    echo "  DynamoDB: $DYNAMO_FILE_TABLE  (PITR enabled, 3 items)"
    echo "  DynamoDB: $DYNAMO_THING_TABLE  (PITR enabled, 3 items)"
    echo
    echo "Backup output buckets will be created automatically by 'backup run':"
    echo "  s3://$TOSHI_S3_BUCKET-backup-$REGION-$ACCOUNT_ID"
    echo "  s3://$THS_S3_BUCKET-backup-$REGION-$ACCOUNT_ID"
    echo "  s3://nzshm-dynamo-backup-toshi-$REGION-$ACCOUNT_ID"
}

# ── Status ────────────────────────────────────────────────────────────────────

cmd_status() {
    require_cmd aws
    ACCOUNT_ID=$(get_account_id)
    echo
    echo "=== Sandbox Status (account: $ACCOUNT_ID, region: $REGION) ==="
    echo

    echo "--- Source S3 buckets ---"
    for BUCKET in "$TOSHI_S3_BUCKET" "$THS_S3_BUCKET"; do
        if bucket_exists "$BUCKET"; then
            COUNT=$(aws s3 ls "s3://$BUCKET" --recursive | wc -l | tr -d ' ')
            echo "  ✓ s3://$BUCKET  ($COUNT objects)"
        else
            echo "  ✗ s3://$BUCKET  (NOT FOUND)"
        fi
    done

    echo
    echo "--- DynamoDB source tables ---"
    for TABLE in "$DYNAMO_FILE_TABLE" "$DYNAMO_THING_TABLE"; do
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
        "$TOSHI_S3_BUCKET-backup-$REGION-$ACCOUNT_ID" \
        "$THS_S3_BUCKET-backup-$REGION-$ACCOUNT_ID" \
        "nzshm-dynamo-backup-toshi-$REGION-$ACCOUNT_ID"; do
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
        echo "$RULES" | while read -r line; do echo "  $line"; done
    fi
    echo
}

# ── Teardown ──────────────────────────────────────────────────────────────────

cmd_teardown() {
    require_cmd aws
    ACCOUNT_ID=$(get_account_id)
    echo
    echo "=== Sandbox Teardown ==="
    echo "Account: $ACCOUNT_ID  Region: $REGION"
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
        "$TOSHI_S3_BUCKET-backup-$REGION-$ACCOUNT_ID" \
        "$THS_S3_BUCKET-backup-$REGION-$ACCOUNT_ID" \
        "nzshm-dynamo-backup-toshi-$REGION-$ACCOUNT_ID" \
        "nzshm-dynamo-backup-ths-$REGION-$ACCOUNT_ID"; do
        if bucket_exists "$BUCKET"; then
            info "Emptying + deleting s3://$BUCKET..."
            # Remove lifecycle policy first (so delete works on Glacier objects)
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
    for TABLE in "$DYNAMO_FILE_TABLE" "$DYNAMO_THING_TABLE"; do
        if table_exists "$TABLE"; then
            info "Deleting DynamoDB table: $TABLE..."
            aws dynamodb delete-table --table-name "$TABLE" --region "$REGION" >/dev/null
            ok "Deleted: $TABLE"
        else
            warn "Not found (skipping): $TABLE"
        fi
    done

    # Delete EventBridge rules created against sandbox sources
    for RULE in \
        "nzshm-backup-toshi-daily" "nzshm-backup-toshi-weekly" \
        "nzshm-backup-ths-daily"   "nzshm-backup-ths-weekly" \
        "nzshm-backup-all-daily"   "nzshm-backup-all-weekly"; do
        RULE_EXISTS=$(aws events list-rules --name-prefix "$RULE" --region "$REGION" \
            --query "Rules[?Name=='$RULE'].Name" --output text 2>/dev/null || echo "")
        if [ -n "$RULE_EXISTS" ]; then
            info "Removing EventBridge targets + rule: $RULE"
            # Remove targets first
            TARGET_IDS=$(aws events list-targets-by-rule --rule "$RULE" --region "$REGION" \
                --query "Targets[].Id" --output text 2>/dev/null || echo "")
            if [ -n "$TARGET_IDS" ]; then
                aws events remove-targets --rule "$RULE" --region "$REGION" \
                    --ids $TARGET_IDS >/dev/null 2>/dev/null || true
            fi
            aws events delete-rule --name "$RULE" --region "$REGION" >/dev/null
            ok "Deleted rule: $RULE"
        fi
    done

    # Remove generated config
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
        echo "  setup     Create sandbox S3 buckets + DynamoDB tables + backup-config.sandbox.yaml"
        echo "  status    Show state of sandbox resources and backup output buckets"
        echo "  teardown  Delete all sandbox resources (with confirmation)"
        exit 1
        ;;
esac
