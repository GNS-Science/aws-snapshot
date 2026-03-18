# Testing

## Test layout

```
tests/
├── conftest.py                 # shared fixtures (moto mocks, config factories)
├── test_backup_engine.py       # BackupEngine integration
├── test_s3_backup.py           # S3 incremental sync, lifecycle policy
├── test_dynamodb_backup.py     # DynamoDB PITR export
├── test_s3_restore.py          # S3 restore (direct copy + S3 Batch path)
├── test_dynamodb_restore.py    # DynamoDB PITR restore
├── test_s3_batch.py            # S3 Batch Operations manifest + job submission
├── test_schedule.py            # EventBridge rule create/remove/enable/disable
├── test_status.py              # status command output
├── test_config.py              # Pydantic model validation
└── test_cli.py                 # CLI smoke tests (Typer CliRunner)
```

## Running tests

```bash
# All tests
poetry run pytest

# Single file
poetry run pytest tests/test_s3_backup.py

# Single test
poetry run pytest tests/test_s3_backup.py::test_incremental_sync_skips_matching_etags

# Verbose output
poetry run pytest -v

# Show captured output (useful for debugging)
poetry run pytest -s

# Stop on first failure
poetry run pytest -x
```

## Mocking AWS services

Tests use [moto](https://docs.getmoto.org/) to mock AWS services locally — no
real AWS calls are made during test runs.

```python
import boto3
import pytest
from moto import mock_aws

@pytest.fixture
def aws_s3(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-southeast-2")
        yield s3
```

`mock_aws()` mocks all AWS services used in the test. Do not mix mocked and
real AWS calls in the same test.

## Config fixtures

Use the `make_config` fixture from `conftest.py` to generate a minimal valid
`ConfigModel` for tests:

```python
def test_backup_dry_run(make_config):
    config = make_config(sources={"toshi": {...}})
    result = run_backup_source(session, config, "toshi", dry_run=True)
    assert result.s3_results[0]["objects_copied"] == 0
```

## Coverage

```bash
poetry run pytest --cov=nzshm_backup --cov-report=term-missing
```

Current coverage: ~152 tests. Target: maintain > 70% line coverage.

## Test categories

| Category | Marker | Notes |
|----------|--------|-------|
| Unit | (default) | All tests — run on every commit |
| Integration | `@pytest.mark.integration` | Requires real AWS credentials — not in CI |

Integration tests are excluded from the default `pytest` run. To run them
explicitly against a sandbox account:

```bash
eval "$(aws configure export-credentials --profile sandbox --format env)"
poetry run pytest -m integration
```
