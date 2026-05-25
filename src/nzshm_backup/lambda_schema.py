"""Pydantic schema for Lambda task invocation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackupTask(BaseModel):
    """Backup task definition for Lambda invocation.

    This schema is used to validate EventBridge events that trigger
    backup operations. It provides a clean interface between the
    scheduler and the backup execution logic.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="Source alias to backup")
    dry_run: bool = Field(False, description="Simulate without executing")
    trigger_type: Literal["scheduled", "manual"] = Field(
        "scheduled", description="What triggered this backup"
    )
    full_sync: bool = Field(False, description="Force full copy instead of incremental sync")
    prepare_only: bool = Field(
        False,
        description="Build S3 Batch manifest only; skip job submission",
    )
    task_type: Literal["backup", "health_report"] = Field(
        "backup",
        description=(
            "Which task this event invokes. Defaults to backup so existing "
            "EventBridge rules created before this discriminator (which omit "
            "the field) continue to dispatch as before. Health-report rules "
            "pass task_type='health_report' and a sentinel source value."
        ),
    )

    def is_scheduled(self) -> bool:
        """Check if this is a scheduled (vs manual) backup."""
        return self.trigger_type == "scheduled"

    def should_sync_all(self) -> bool:
        """Check if full sync is required."""
        return self.full_sync
