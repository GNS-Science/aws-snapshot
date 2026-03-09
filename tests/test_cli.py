"""Smoke tests for the backup CLI entry point and subcommands."""

import pytest
from typer.testing import CliRunner

from nzshm_backup import __version__
from nzshm_backup.cli import app

runner = CliRunner()


def test_help_exits_cleanly():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "NSHM Backup Solution" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.parametrize("subcommand", [
    ["schedule", "--help"],
    ["run", "--help"],
    ["restore", "--help"],
    ["test", "--help"],
    ["status", "--help"],
    ["report", "--help"],
    ["costs", "--help"],
    ["config", "--help"],
])
def test_subcommand_help(subcommand):
    result = runner.invoke(app, subcommand)
    assert result.exit_code == 0


def test_dry_run_flag_propagates():
    result = runner.invoke(app, ["--dry-run", "run", "--source", "toshi"])
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output


def test_run_without_dry_run():
    result = runner.invoke(app, ["run", "--source", "ths"])
    assert result.exit_code == 0
    assert "DRY RUN" not in result.output


def test_costs_subcommands_exist():
    for sub in ["predict", "report", "breakdown", "export"]:
        result = runner.invoke(app, ["costs", sub, "--help"])
        assert result.exit_code == 0, f"costs {sub} --help failed: {result.output}"


def test_report_compliance_exists():
    result = runner.invoke(app, ["report", "compliance", "--help"])
    assert result.exit_code == 0
