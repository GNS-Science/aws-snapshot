"""Setup/provisioning commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import typer
from botocore.exceptions import ClientError
from pydantic import ValidationError

from aws_snapshot.config import load_config
from aws_snapshot.s3_backup import (
    LifecycleConfig,
    apply_lifecycle_policy,
    bucket_exists,
    get_account_id,
)

app = typer.Typer(help="Provision and configure backup infrastructure components.")
inventory_app = typer.Typer(help="Configure inventory producers for backup sources.")
iam_app = typer.Typer(help="Configure IAM roles and policies.")

app.add_typer(inventory_app, name="inventory")
app.add_typer(iam_app, name="iam")


def _script_path(script_name: str) -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / script_name


def _run_script(args: list[str]) -> None:
    proc = subprocess.run(args, check=False)
    if proc.returncode != 0:
        raise typer.Exit(proc.returncode)


@inventory_app.callback(invoke_without_command=True)
def setup_inventory(
    source: str = typer.Option(..., help="Source alias in config"),
    config: str = typer.Option(
        os.getenv("BACKUP_CONFIG_PATH", "backup-config.yaml"),
        help="Config file path",
    ),
    source_profile: str = typer.Option(..., help="AWS profile for source account"),
    backup_profile: str = typer.Option(..., help="AWS profile for backup account"),
    control_bucket: str | None = typer.Option(None, help="Inventory destination bucket override"),
    control_prefix: str = typer.Option("inventory", help="Inventory destination root prefix"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show intended changes only"),
) -> None:
    """Set up daily Parquet inventories for source and backup buckets of a source."""
    script = _script_path("setup-inventory.py")
    cmd = [
        sys.executable,
        str(script),
        "--config",
        config,
        "--source",
        source,
        "--source-profile",
        source_profile,
        "--backup-profile",
        backup_profile,
        "--control-prefix",
        control_prefix,
    ]
    if control_bucket:
        cmd += ["--control-bucket", control_bucket]
    if dry_run:
        cmd += ["--dry-run"]
    _run_script(cmd)


@app.command("lifecycle")
def setup_lifecycle(
    source: str = typer.Option("all", help="Source alias to update, or 'all'"),
    config_path: str = typer.Option(
        os.getenv("BACKUP_CONFIG_PATH", "backup-config.yaml"),
        "--config",
        help="Config file path",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print intended policy without applying"),
) -> None:
    """Re-apply the lifecycle policy to deployed backup buckets.

    Bucket-creation is the only place ``apply_lifecycle_policy`` runs today, so
    a change to ``RetentionConfig`` (e.g. ADR-006) does not propagate to
    already-deployed buckets via ``backup run``. This command walks the
    configured S3 and DynamoDB backup buckets for the selected source(s) and
    pushes the policy derived from ``config.retention``.
    """
    try:
        cfg = load_config(Path(config_path))
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
    except ValidationError as e:
        typer.echo(f"Error: invalid config — {e}", err=True)
        raise typer.Exit(1) from e

    if source == "all":
        sources_to_update = list(cfg.sources.keys())
    else:
        if source not in cfg.sources:
            valid = ", ".join(sorted(cfg.sources.keys()))
            typer.echo(f"Error: unknown source '{source}'. Valid sources: {valid}", err=True)
            raise typer.Exit(1)
        sources_to_update = [source]

    lifecycle_config = LifecycleConfig(
        hot_days=cfg.retention.hot_days,
        version_retention_days=cfg.retention.version_retention_days,
    )

    session = boto3.Session()
    backup_account_id = get_account_id(session)
    region = cfg.general.region
    s3_client = session.client("s3")

    # Bucket names embed the *source* account id (see backup_engine.py:72,81),
    # not the backup-account caller. Falls back to backup account for same-
    # account sources.
    bucket_names: list[str] = []
    for alias in sources_to_update:
        src_cfg = cfg.sources[alias]
        naming_account_id = src_cfg.source_account_id or backup_account_id
        for b in src_cfg.s3_buckets:
            bucket_names.append(
                src_cfg.get_backup_bucket_name(b.label, region, naming_account_id, alias)
            )
        if src_cfg.dynamodb_tables:
            bucket_names.append(
                src_cfg.get_dynamodb_backup_bucket_name(alias, region, naming_account_id)
            )

    typer.echo(
        f"Lifecycle policy: hot_days={lifecycle_config.hot_days}, "
        f"version_retention_days={lifecycle_config.version_retention_days}"
    )
    typer.echo(f"Buckets ({len(bucket_names)}):")
    for name in bucket_names:
        typer.echo(f"  - {name}")

    if dry_run:
        rule: dict = {
            "ID": "BackupTierTransition",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Transitions": [{"Days": lifecycle_config.hot_days, "StorageClass": "GLACIER_IR"}],
        }
        if lifecycle_config.version_retention_days > 0:
            rule["NoncurrentVersionExpiration"] = {
                "NoncurrentDays": lifecycle_config.version_retention_days
            }
        typer.echo("\nIntended policy (dry-run):")
        typer.echo(json.dumps({"Rules": [rule]}, indent=2))
        return

    any_failed = False
    for name in bucket_names:
        if not bucket_exists(s3_client, name):
            typer.echo(f"  [SKIP] {name}: does not exist")
            any_failed = True
            continue
        try:
            apply_lifecycle_policy(s3_client, name, lifecycle_config)
            typer.echo(f"  [OK]   {name}")
        except ClientError as e:
            typer.echo(f"  [FAIL] {name}: {e}", err=True)
            any_failed = True

    if any_failed:
        raise typer.Exit(1)


@iam_app.command("source-roles")
def setup_source_roles(
    source: str = typer.Option(..., help="Source alias in config"),
    profile: str = typer.Option(..., help="AWS profile for source account"),
    config: str = typer.Option(
        os.getenv("BACKUP_CONFIG_PATH", "backup-config.yaml"),
        help="Config file path",
    ),
    batch_role_arn: str | None = typer.Option(None, help="Override S3 Batch role ARN"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show intended changes only"),
) -> None:
    """Create/update source account reader/restore roles and bucket policies."""
    script = _script_path("create-source-roles.py")
    cmd = [
        sys.executable,
        str(script),
        "--config",
        config,
        "--source",
        source,
        "--profile",
        profile,
    ]
    if batch_role_arn:
        cmd += ["--batch-role-arn", batch_role_arn]
    if dry_run:
        cmd += ["--dry-run"]
    _run_script(cmd)


@iam_app.command("backup-batch-role")
def setup_backup_roles(
    profile: str = typer.Option(..., help="AWS profile for backup account"),
    config: str = typer.Option(
        os.getenv("BACKUP_CONFIG_PATH", "backup-config.yaml"),
        help="Config file path",
    ),
    no_write_back: bool = typer.Option(
        False,
        "--no-write-back",
        help="Do not update config with generated role ARN",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show intended changes only"),
) -> None:
    """Create/update backup-account S3 Batch role and policy."""
    script = _script_path("create-backup-roles.py")
    cmd = [sys.executable, str(script), "--config", config, "--profile", profile]
    if no_write_back:
        cmd += ["--no-write-back"]
    if dry_run:
        cmd += ["--dry-run"]
    _run_script(cmd)
