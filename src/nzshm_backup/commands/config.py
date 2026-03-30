"""Configuration management commands."""

import json
from pathlib import Path

import boto3
import typer

from nzshm_backup.config import ConfigModel, load_config, save_config
from nzshm_backup.config.loader import load_config_from_ssm
from nzshm_backup.state import get_state

app = typer.Typer()


def _get_config_path() -> Path:
    """Get config file path from state or default."""
    state = get_state()
    return getattr(state, "config_path", Path("backup-config.yaml"))


@app.command("show")
def show_config(
    key: str | None = typer.Argument(None, help="Configuration key (show all if not provided)"),
):
    """Show configuration values.

    If KEY is provided, show specific value. Otherwise show all config.
    """
    try:
        config = load_config(_get_config_path())
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    config_dict = config.model_dump(mode="json", by_alias=True)

    if key:
        keys = key.split(".")
        value = config_dict
        try:
            for k in keys:
                value = value[k]
            typer.echo(str(value))
        except (KeyError, TypeError) as e:
            typer.echo(f"Error: Key '{key}' not found", err=True)
            raise typer.Exit(1) from e
    else:
        import yaml  # type: ignore[import-untyped]

        typer.echo(yaml.dump(config_dict, default_flow_style=False, sort_keys=False))


@app.command("set")
def set_config(
    key: str = typer.Argument(..., help="Configuration key"),
    value: str = typer.Argument(..., help="Configuration value"),
):
    """Set configuration value."""
    config_path = _get_config_path()

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        config = ConfigModel(sources={})

    keys = key.split(".")
    config_dict = config.model_dump(mode="json", by_alias=True)

    current = config_dict
    for k in keys[:-1]:
        if k not in current:
            typer.echo(f"Error: Key '{key}' not found", err=True)
            raise typer.Exit(1)
        current = current[k]

    current[keys[-1]] = value

    new_config = ConfigModel.model_validate(config_dict)
    save_config(new_config, config_path)
    typer.echo(f"Set {key} = {value}")


@app.command("validate")
def validate_config():
    """Validate configuration file."""
    config_path = _get_config_path()

    try:
        config = load_config(config_path)
        typer.echo(f"Configuration valid: {config_path}")
        typer.echo(f"Sources configured: {', '.join(config.sources.keys())}")
    except FileNotFoundError as e:
        typer.echo(f"Error: Config file not found: {config_path}", err=True)
        raise typer.Exit(1) from e
    except Exception as e:
        typer.echo(f"Error: Invalid configuration: {e}", err=True)
        raise typer.Exit(1) from e


@app.command("push")
def push_config(
    config_file: Path = typer.Argument(
        None, help="Path to config file (default: BACKUP_CONFIG_PATH or backup-config.yaml)"
    ),
    stage: str = typer.Option("dev", help="Deployment stage (e.g. dev, prod)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be pushed without pushing"
    ),
):
    """Push local config to SSM Parameter Store."""
    if config_file is None:
        config_file = _get_config_path()

    try:
        config = load_config(config_file)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    param_name = f"/nzshm-backup/{stage}/config"
    json_str = json.dumps(config.model_dump(mode="json", by_alias=True))

    typer.echo(f"Source: {config_file.resolve()}")
    if dry_run:
        typer.echo(f"[dry-run] Would push config to SSM parameter: {param_name}")
        typer.echo(f"[dry-run] Config JSON ({len(json_str)} bytes):")
        typer.echo(json_str)
        return

    ssm = boto3.client("ssm")
    ssm.put_parameter(
        Name=param_name,
        Value=json_str,
        Type="String",
        Overwrite=True,
    )
    typer.echo(f"Config pushed to SSM parameter: {param_name}")


@app.command("pull")
def pull_config(
    stage: str = typer.Option("dev", help="Deployment stage (e.g. dev, prod)"),
    save: bool = typer.Option(False, "--save", help="Save pulled config to local config file"),
):
    """Pull config from SSM Parameter Store and display it."""
    try:
        config = load_config_from_ssm(stage)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    import yaml

    config_dict = config.model_dump(mode="json", by_alias=True)
    typer.echo(yaml.dump(config_dict, default_flow_style=False, sort_keys=False))

    if save:
        config_path = _get_config_path()
        save_config(config, config_path)
        typer.echo(f"Config saved to: {config_path}")
