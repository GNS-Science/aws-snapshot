"""Pydantic models for backup configuration."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class S3BucketConfig(BaseModel):
    """Configuration for a single S3 bucket source."""

    arn: str = Field(..., description="S3 bucket ARN")
    label: str = Field(..., description="Short human-readable label used in backup bucket name")


class RetentionConfig(BaseModel):
    """Retention policy configuration."""

    hot_days: int = 30
    warm_days: int = 120  # must be >= hot_days + 90 (AWS constraint for GLACIER_IR → DEEP_ARCHIVE)
    max_age_days: int = 365
    version_retention_days: int = 365  # how long superseded object versions are kept; 0 = forever


class RestoreConfig(BaseModel):
    """Restore operation configuration."""

    default_destination_type: Literal["temporary", "permanent"] = "temporary"
    temporary_retention_days: int = 7
    dynamodb_always_new_table: bool = True
    auto_approve_threshold: float = 100.0  # NZD
    dual_approval_threshold: float = 500.0  # NZD


class SlackConfig(BaseModel):
    """Slack notification configuration."""

    enabled: bool = True
    webhook_url_secret: str = "backup-slack-webhook"
    channel: str = "#nsdm-backups"
    notify_on: list[
        Literal[
            "backup_success",
            "backup_failure",
            "restore_initiated",
            "restore_completed",
            "test_failure",
        ]
    ] = [
        "backup_success",
        "backup_failure",
        "restore_initiated",
        "restore_completed",
    ]


class SESConfig(BaseModel):
    """SES email notification configuration."""

    enabled: bool = True
    source_email: str = "noreply-backup@example.com"
    recipients: list[str] = []


class AlertsConfig(BaseModel):
    """Lambda error alarm fast-path configuration.

    Drives the CloudWatch alarm -> SNS topic. Subscriptions are managed
    by ``backup notifications apply`` (NOT serverless.yml) — see the
    notifications runbook for the apply workflow.
    """

    emails: list[str] = []


class ReportsEmailConfig(BaseModel):
    """SNS-based email delivery for the daily health report.

    Drives publication to BackupReportsTopic when ``enabled`` is true.
    Subscriptions to that topic are managed by
    ``backup notifications apply`` (NOT serverless.yml). SES is
    deliberately not used — see ADR-005 (revised).
    """

    enabled: bool = False
    addresses: list[str] = []


class HealthReportConfig(BaseModel):
    """Tunable thresholds and rotation for the daily health report.

    All fields are optional with defaults that match the previously-
    hardcoded values in src/nzshm_backup/health_report.py.

    Map keys are ISO weekday numbers (0=Mon … 6=Sun). Add or remove
    entries to change which large source gets restore-tested on which
    day; default: Mon=ths, Wed=toshi, Fri=static (other days only the
    canary runs).
    """

    canary_source: str = "weka"
    rotation_by_weekday: dict[int, str] = Field(
        default_factory=lambda: {0: "ths", 2: "toshi", 4: "static"}
    )
    freshness_threshold_hours: float = 30.0
    delta_pct_threshold: float = -5.0  # source-count drop ≥ 5% (negative) → red
    delta_abs_threshold: int = -10_000  # source-count drop ≥ 10k objects → red
    restore_sample_size: int = 10


class ReportsConfig(BaseModel):
    """Daily-report delivery + tuning configuration."""

    email: ReportsEmailConfig = Field(default_factory=ReportsEmailConfig)
    health: HealthReportConfig = Field(default_factory=HealthReportConfig)


class NotificationConfig(BaseModel):
    """Notification configuration."""

    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    ses: SESConfig = Field(default_factory=SESConfig)
    slack: SlackConfig | None = None

    def model_post_init(self, __context) -> None:
        if self.slack is None:
            self.slack = SlackConfig()


class CostTrackingConfig(BaseModel):
    """Cost tracking configuration."""

    enabled: bool = True
    budget_alerts: bool = True
    monthly_budget: float = 700.0  # NZD
    export_to_s3: str | None = None


class SourceConfig(BaseModel):
    """Configuration for a single backup source."""

    display_name: str = Field(..., description="Human-readable name")
    s3_buckets: list[S3BucketConfig] = Field(default_factory=list, description="S3 bucket configs")
    dynamodb_tables: list[str] = Field(default_factory=list, description="DynamoDB table ARNs")
    dynamodb_export_format: Literal["DYNAMODB_JSON", "ION"] = "DYNAMODB_JSON"
    source_account_role_arn: str | None = Field(
        None,
        description="IAM role ARN in the source account to assume for cross-account read access "
        "(S3 backup, DynamoDB export). If None, the Lambda's own credentials are used.",
    )
    source_account_restore_role_arn: str | None = Field(
        None,
        description="IAM role ARN in the source account to assume for cross-account restore "
        "operations (RestoreTableToPointInTime, PITR re-enable, tag management). "
        "Required for cross-account DynamoDB restores. "
        "If None, falls back to source_account_role_arn.",
    )
    source_account_id: str | None = Field(
        None,
        description="AWS account ID that owns the source data. "
        "Required for cross-account sources. Validated against source_account_role_arn.",
    )
    use_s3_batch: bool = Field(
        False,
        description="Use S3 Batch Operations instead of per-object copy_object. "
        "Required for large buckets (millions of objects). Requires general.s3_batch_role_arn.",
    )
    batch_manifest_mode: Literal["inline", "inventory"] = Field(
        "inline",
        description="How S3 Batch manifests are prepared: "
        "'inline' lists source+backup buckets live; "
        "'inventory' diffs latest S3 Inventory snapshots.",
    )

    def get_backup_bucket_name(
        self, bucket_label: str, region: str, account_id: str, source_key: str
    ) -> str:
        """Generate human-readable backup bucket name from source key and bucket label."""
        return f"bb-{source_key}-s3-{bucket_label}-{region}-{account_id}"

    def get_dynamodb_backup_bucket_name(self, source_key: str, region: str, account_id: str) -> str:
        """Generate human-readable DynamoDB export bucket name."""
        return f"bb-{source_key}-dynamo-{region}-{account_id}"


class GeneralConfig(BaseModel):
    """General configuration."""

    region: Literal["ap-southeast-2"] = "ap-southeast-2"
    environment: Literal["production", "staging", "development"] = "production"
    tags: dict[str, str] = Field(
        default_factory=lambda: {"Project": "NSHM", "ManagedBy": "backup-cli"}
    )
    lambda_arn: str | None = Field(default=None, description="ARN of the backup Lambda function")
    s3_batch_role_arn: str | None = Field(
        default=None,
        description="ARN of the IAM role S3 Batch Operations assumes. "
        "Required when any source has use_s3_batch: true.",
    )


class ConfigModel(BaseModel):
    """Root configuration model."""

    general: GeneralConfig = Field(default_factory=lambda: GeneralConfig())
    sources: dict[str, SourceConfig]
    retention: RetentionConfig = Field(default_factory=lambda: RetentionConfig())
    restore: RestoreConfig = Field(default_factory=lambda: RestoreConfig())
    notifications: NotificationConfig = Field(default_factory=lambda: NotificationConfig())
    cost_tracking: CostTrackingConfig = Field(default_factory=lambda: CostTrackingConfig())

    @model_validator(mode="after")
    def validate_batch_config(self) -> "ConfigModel":
        if any(s.use_s3_batch for s in self.sources.values()):
            if not self.general.s3_batch_role_arn:
                raise ValueError(
                    "general.s3_batch_role_arn is required when any source has use_s3_batch: true"
                )
        return self

    @model_validator(mode="after")
    def validate_source_accounts(self) -> "ConfigModel":
        for alias, source in self.sources.items():
            if source.source_account_role_arn and not source.source_account_id:
                raise ValueError(
                    f"sources.{alias}: source_account_id is required"
                    " when source_account_role_arn is set"
                )
            if source.source_account_role_arn and source.source_account_id:
                arn_account = source.source_account_role_arn.split(":")[4]
                if arn_account != source.source_account_id:
                    raise ValueError(
                        f"sources.{alias}: source_account_id {source.source_account_id!r} "
                        f"does not match account in source_account_role_arn ({arn_account!r})"
                    )
            if source.source_account_restore_role_arn and source.source_account_id:
                arn_account = source.source_account_restore_role_arn.split(":")[4]
                if arn_account != source.source_account_id:
                    raise ValueError(
                        f"sources.{alias}: source_account_id {source.source_account_id!r} "
                        f"does not match account in source_account_restore_role_arn"
                        f" ({arn_account!r})"
                    )
            if source.source_account_id:
                for table_arn in source.dynamodb_tables:
                    arn_account = table_arn.split(":")[4]
                    if arn_account != source.source_account_id:
                        raise ValueError(
                            f"sources.{alias}: DynamoDB table {table_arn!r} belongs to account "
                            f"{arn_account!r}, expected {source.source_account_id!r}"
                        )
            labels = [b.label for b in source.s3_buckets]
            if len(labels) != len(set(labels)):
                raise ValueError(f"sources.{alias}: s3_bucket labels must be unique")
        return self
