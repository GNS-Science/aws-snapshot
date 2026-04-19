"""Pre-flight access and configuration checks."""

import boto3
import typer
from botocore.exceptions import ClientError
from pydantic import ValidationError

from nzshm_backup.config import load_config
from nzshm_backup.config.models import ConfigModel
from nzshm_backup.s3_backup import get_cross_account_session

app = typer.Typer()

_PASS = "PASS"
_FAIL = "FAIL"
_WARN = "WARN"


def _row(label: str, status: str, detail: str = "") -> None:
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m"}.get(status, "")
    reset = "\033[0m"
    suffix = f"  {detail}" if detail else ""
    typer.echo(f"  [{colour}{status}{reset}] {label}{suffix}")


@app.callback(invoke_without_command=True)
def check(
    source: str = typer.Option("all", help="Source alias to check, or 'all'"),
) -> None:
    """Validate IAM access, bucket reachability, and DynamoDB PITR for all configured sources.

    Runs fast pre-flight checks without enumerating objects. Use this before
    'backup run' to confirm credentials and permissions are correct.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
    except ValidationError as e:
        typer.echo(f"Error: invalid config — {e}", err=True)
        raise typer.Exit(1) from e

    if source == "all":
        sources_to_check = list(config.sources.keys())
    else:
        if source not in config.sources:
            valid = ", ".join(sorted(config.sources.keys()))
            typer.echo(f"Error: unknown source '{source}'. Valid sources: {valid}", err=True)
            raise typer.Exit(1)
        sources_to_check = [source]

    session = boto3.Session()
    any_fail = False

    for alias in sources_to_check:
        typer.echo(f"\nSource: {alias}")
        failed = _check_source(session, config, alias)
        if failed:
            any_fail = True

    typer.echo("")
    if any_fail:
        typer.echo("One or more checks FAILED — review errors above before running backup.")
        raise typer.Exit(1)
    else:
        typer.echo("All checks passed.")


def _check_source(session: boto3.Session, config: ConfigModel, alias: str) -> bool:
    """Run all checks for a single source. Returns True if any check failed."""
    source_cfg = config.sources[alias]
    region = config.general.region
    failed = False

    # --- Backup account identity ---
    try:
        identity = session.client("sts").get_caller_identity()
        backup_account_id = identity["Account"]
        _row("Backup account credentials", _PASS, backup_account_id)
    except ClientError as e:
        _row("Backup account credentials", _FAIL, str(e))
        return True  # nothing else will work

    # --- Cross-account role assumption ---
    source_session: boto3.Session | None = None
    source_account_id = source_cfg.source_account_id or backup_account_id

    if source_cfg.source_account_role_arn:
        try:
            source_session = get_cross_account_session(session, source_cfg.source_account_role_arn)
            assumed_id = source_session.client("sts").get_caller_identity()
            _row(
                f"Assume role {source_cfg.source_account_role_arn.split('/')[-1]}",
                _PASS,
                assumed_id.get("Arn", ""),
            )
        except ClientError as e:
            _row("Assume source role", _FAIL, str(e))
            failed = True
            source_session = None
    else:
        _row("Cross-account role", _WARN, "not configured (same-account backup)")

    s3_client = (source_session or session).client("s3")

    # --- S3 bucket access ---
    for bucket_cfg in source_cfg.s3_buckets:
        bucket = bucket_cfg.arn.split(":::")[-1]
        backup_bucket = source_cfg.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, alias
        )

        # Source bucket read access
        try:
            s3_client.list_objects_v2(Bucket=bucket, MaxKeys=1)
            _row(f"Read {bucket}", _PASS)
        except ClientError as e:
            _row(f"Read {bucket}", _FAIL, e.response["Error"]["Code"])
            failed = True

        # Backup bucket existence (in backup account)
        backup_s3 = session.client("s3")
        try:
            backup_s3.head_bucket(Bucket=backup_bucket)
            _row(f"Backup bucket {backup_bucket}", _PASS, "exists")

            # Guardrail: existing backup buckets must have versioning enabled.
            try:
                versioning = backup_s3.get_bucket_versioning(Bucket=backup_bucket)
                status = versioning.get("Status")
                if status == "Enabled":
                    _row(f"Versioning {backup_bucket}", _PASS, "Enabled")
                else:
                    observed = status or "Disabled"
                    _row(
                        f"Versioning {backup_bucket}",
                        _FAIL,
                        f"status={observed} — enable before backup",
                    )
                    failed = True
            except ClientError as e:
                code = e.response["Error"]["Code"]
                _row(f"Versioning {backup_bucket}", _FAIL, code)
                failed = True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("404", "NoSuchBucket"):
                _row(
                    f"Backup bucket {backup_bucket}",
                    _WARN,
                    "does not exist yet (will be created on first run)",
                )
            else:
                _row(f"Backup bucket {backup_bucket}", _FAIL, code)
                failed = True

    # --- S3 Batch role ---
    if source_cfg.use_s3_batch:
        batch_role_arn = config.general.s3_batch_role_arn
        if not batch_role_arn:
            _row("S3 Batch role", _FAIL, "use_s3_batch=true but s3_batch_role_arn not set")
            failed = True
        else:
            try:
                iam = session.client("iam")
                role_name = batch_role_arn.split("/")[-1]
                iam.get_role(RoleName=role_name)
                _row(f"S3 Batch role {role_name}", _PASS)
            except ClientError as e:
                _row("S3 Batch role", _FAIL, e.response["Error"]["Code"])
                failed = True

    # --- DynamoDB PITR ---
    if source_cfg.dynamodb_tables:
        dynamo_client = (source_session or session).client("dynamodb", region_name=region)
        for table_arn in source_cfg.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            try:
                resp = dynamo_client.describe_continuous_backups(TableName=table_name)
                pitr = resp["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
                status = pitr["PointInTimeRecoveryStatus"]
                if status == "ENABLED":
                    _row(f"PITR {table_name}", _PASS)
                else:
                    _row(
                        f"PITR {table_name}", _WARN, f"status={status} — enable PITR before backup"
                    )
            except ClientError as e:
                _row(f"PITR {table_name}", _FAIL, e.response["Error"]["Code"])
                failed = True

    return failed
