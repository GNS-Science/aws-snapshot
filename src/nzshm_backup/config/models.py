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
    cold_days: int = 365
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


class NotificationConfig(BaseModel):
    """Notification configuration."""

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


class TestingConfig(BaseModel):
    """Automated testing configuration."""

    class WeeklyTest(BaseModel):
        enabled: bool = True
        day: str = "wednesday"
        time: str = "10:00"
        sample_size_mb: int = 100

    class MonthlyRestore(BaseModel):
        enabled: bool = True
        day: str = "first-monday"
        time: str = "09:00"
        table: str = "ToshiAPI-FileTable"

    class QuarterlyDrill(BaseModel):
        enabled: bool = True
        months: list[Literal["january", "april", "july", "october"]] = [
            "january",
            "april",
            "july",
            "october",
        ]
        day: int = 15
        isolated_environment: bool = True

    weekly_small_test: WeeklyTest = Field(default_factory=lambda: TestingConfig.WeeklyTest())
    monthly_table_restore: MonthlyRestore = Field(
        default_factory=lambda: TestingConfig.MonthlyRestore()
    )
    quarterly_full_drill: QuarterlyDrill = Field(
        default_factory=lambda: TestingConfig.QuarterlyDrill()
    )


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
    testing: TestingConfig = Field(default_factory=lambda: TestingConfig())

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
