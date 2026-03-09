"""Configuration loader for NSHM Backup."""

import os
from pathlib import Path

import yaml

from nzshm_backup.config.models import ConfigModel

DEFAULT_CONFIG_PATH = Path("backup-config.yaml")
CONFIG_PATH_ENV_VAR = "BACKUP_CONFIG_PATH"


def load_config(config_path: Path | None = None) -> ConfigModel:
    """Load configuration from YAML file.

    Config path resolution order:
    1. Explicit `config_path` argument
    2. BACKUP_CONFIG_PATH environment variable
    3. ./backup-config.yaml (default)

    Args:
        config_path: Path to config file.

    Returns:
        Validated ConfigModel instance

    Raises:
        FileNotFoundError: If config file doesn't exist
        yaml.YAMLError: If config file is invalid YAML
        pydantic.ValidationError: If config schema validation fails
    """
    if config_path is None:
        env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
        config_path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH

    config_path = Path(config_path).expanduser().resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    return ConfigModel.model_validate(config_data)


def save_config(config: ConfigModel, config_path: Path | None = None) -> None:
    """Save configuration to YAML file.

    Args:
        config: ConfigModel instance to save
        config_path: Path to config file. Defaults to ./backup-config.yaml
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    config_path = Path(config_path).expanduser().resolve()

    config_data = config.model_dump(mode="json", by_alias=True, exclude_none=True)

    with open(config_path, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)


def get_config(config_path: Path | None = None) -> ConfigModel:
    """Load configuration with caching.

    For CLI usage, loads from file. For Lambda, expects config to be
    passed via event or environment.

    Args:
        config_path: Path to config file (CLI only)

    Returns:
        Validated ConfigModel instance
    """
    return load_config(config_path)


def load_config_from_env(env_var: str = "BACKUP_CONFIG") -> ConfigModel:
    """Load configuration from environment variable (Lambda runtime).

    Args:
        env_var: Environment variable name containing JSON config

    Returns:
        Validated ConfigModel instance

    Raises:
        ValueError: If environment variable not set
        json.JSONDecodeError: If JSON is invalid
        pydantic.ValidationError: If schema validation fails
    """
    import json

    config_json = os.environ.get(env_var)
    if not config_json:
        raise ValueError(f"Environment variable {env_var} not set")

    config_data = json.loads(config_json)
    return ConfigModel.model_validate(config_data)
