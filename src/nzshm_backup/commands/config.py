"""Configuration management commands."""

import click


@click.group("config")
def config():
    """Manage backup configuration."""
    pass


@config.command("show")
@click.argument("key", required=False)
@click.pass_context
def show(ctx, key):
    """Show configuration values.

    If KEY is provided, show specific value. Otherwise show all config.
    """
    if key:
        click.echo(f"Config value for '{key}' - coming soon")
    else:
        click.echo("Showing all configuration - coming soon")


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def set(ctx, key, value):
    """Set configuration value."""
    click.echo(f"Setting {key}={value} - coming soon")


@config.command("validate")
@click.pass_context
def validate(ctx):
    """Validate configuration file."""
    click.echo("Validating configuration - coming soon")
