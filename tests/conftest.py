"""Shared pytest fixtures for nzshm-backup tests."""

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Typer/Click test runner."""
    return CliRunner()


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
