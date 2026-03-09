"""Configuration management commands."""

from pathlib import Path

import typer

from nzshm_backup.config import ConfigModel, load_config, save_config
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
        import yaml

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
