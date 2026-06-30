"""Shared pytest fixtures for nzshm-backup tests."""

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Typer/Click test runner."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Unset shell env vars that leak from a developer's real AWS session.

    A non-exhaustive ``AWS_PROFILE`` / ``AWS_CONFIG_FILE`` unset used to
    let credential env vars leak through — tests that instantiate
    ``boto3.Session`` (or call boto3 directly without the
    ``mock_s3`` / ``mock_dynamodb`` fixtures) would then pick up the
    operator's ambient credentials. When the operator's SSO session
    was expired the test suite failed with ``InvalidClientTokenId``
    instead of running offline.

    Unset every AWS_* env var pytest might inherit; the tox ``[testenv]``
    block sets fake values back as a second layer of defence.
    """
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_ENDPOINT_URL",
        "BACKUP_CONFIG_PATH",
        "BACKUP_CONFIG",
        "NZSHM_STAGE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def aws_credentials(monkeypatch):
    """Fake AWS credentials so boto3 calls never hit real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")


@pytest.fixture
def mock_s3(aws_credentials):
    """Mocked S3 service via moto."""
    with mock_aws():
        yield boto3.client("s3", region_name="ap-southeast-2")


@pytest.fixture
def mock_dynamodb(aws_credentials):
    """Mocked DynamoDB service via moto."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="ap-southeast-2")
