"""Setup/provisioning commands."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

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
