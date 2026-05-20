"""Testing and validation commands."""

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.event_log import append_event
from nzshm_backup.integrity import (
    OPERATIONAL_PREFIXES,
    check_bucket_integrity,
    get_object_checksum,
)
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.s3_batch import batch_restore_bucket, wait_for_batch_job
from nzshm_backup.state import get_state

# ---------------------------------------------------------------------------
# Programmatic restore-test result types
# (used by the daily health report Lambda; CLI also calls the same path)
# ---------------------------------------------------------------------------


@dataclass
class BucketRestoreResult:
    """Per-bucket restore-test outcome."""

    source_bucket: str
    backup_bucket: str
    result: Literal["passed", "failed", "skipped"]
    sample_count: int
    sampled_keys: list[str] = field(default_factory=list)
    copy_errors: list[str] = field(default_factory=list)
    etag_mismatches: list[str] = field(default_factory=list)
    note: str | None = None  # e.g. "no copyable objects" for skipped


@dataclass
class RestoreTestResult:
    """All buckets for one source rolled up."""

    source: str
    mode: Literal["direct copy", "batch"]
    buckets: list[BucketRestoreResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def overall(self) -> Literal["passed", "failed", "skipped"]:
        if not self.buckets:
            return "skipped"
        if any(b.result == "failed" for b in self.buckets):
            return "failed"
        if all(b.result == "skipped" for b in self.buckets):
            return "skipped"
        return "passed"


def _fmt_dt(dt: datetime | str) -> str:
    """Format datetime (or ISO string) in local timezone."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


app = typer.Typer()

_ARCHIVED_STORAGE_CLASSES = {"GLACIER", "GLACIER_IR", "DEEP_ARCHIVE"}


def _verify_restored_object(
    s3_client,
    source_bucket: str,
    target_bucket: str,
    key: str,
    expected_etag: str,
) -> str | None:
    """Verify a restored object against its backup source.

    Tries checksum comparison first (content-deterministic), falls back to ETag.
    Returns an error description string, or None if verification passed.
    """
    # Try checksum comparison first (reliable across copy methods)
    src_ck = get_object_checksum(s3_client, source_bucket, key)
    if src_ck:
        tgt_ck = get_object_checksum(s3_client, target_bucket, key)
        if tgt_ck and src_ck[0] == tgt_ck[0]:
            # Same algorithm — compare values
            if src_ck[1] == tgt_ck[1]:
                return None  # checksum match — verified
            return f"{src_ck[0]} mismatch: {src_ck[1]} != {tgt_ck[1]}"

    # Fall back to ETag comparison
    head = s3_client.head_object(Bucket=target_bucket, Key=key)
    if head["ETag"] != expected_etag:
        return f"ETag mismatch: {head['ETag']} != {expected_etag}"
    return None


@app.command("integrity")
def test_integrity(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
):
    """Validate backup integrity: S3 source ↔ backup comparison, DynamoDB PITR + export check.

    S3: reports missing objects (in source but not in backup) and ETag mismatches
    (possible backup poisoning — source mutation propagated to backup).

    DynamoDB: checks PITR is still enabled on each table and that at least one
    recent completed export exists.

    Exits with code 1 if any discrepancies or missing protection are found.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    source_config = config.sources[source]
    region = config.general.region
    session = boto3.Session()
    account_id = get_account_id(session)
    source_account_id = source_config.source_account_id or account_id

    source_session = (
        get_cross_account_session(session, source_config.source_account_role_arn)
        if source_config.source_account_role_arn
        else None
    )

    backup_s3 = session.client("s3")
    source_s3 = source_session.client("s3") if source_session else backup_s3

    any_failure = False
    typer.echo(f"\n[{source}] Integrity check\n")

    # ------------------------------------------------------------------
    # S3 buckets
    # ------------------------------------------------------------------
    for bucket_cfg in source_config.s3_buckets:
        source_bucket = bucket_cfg.arn.split(":::")[-1]
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )

        typer.echo(f"  S3: {source_bucket}  ↔  {backup_bucket}")
        if source_config.batch_manifest_mode == "inventory":
            typer.echo(
                f"    ⚠ Integrity check uses full bucket listing — may be very slow\n"
                f"      for inventory-mode sources with millions of objects.\n"
                f"      Use 'backup status --source {source}' for inventory-based health.",
                err=True,
            )
        result = check_bucket_integrity(backup_s3, source_bucket, backup_bucket, source_s3)

        typer.echo(
            f"    source: {result.source_object_count} objects  "
            f"backup: {result.backup_object_count} objects"
        )

        if result.clean:
            typer.echo("    ✓ clean — no missing or mismatched objects")
        else:
            any_failure = True
            if result.missing_count:
                typer.echo(f"    ✗ {result.missing_count} object(s) missing from backup:")
                for diff in result.diffs:
                    if diff.issue == "missing_in_backup":
                        typer.echo(f"      - {diff.key}")
            if result.mismatch_count:
                typer.echo(
                    f"    ✗ {result.mismatch_count} ETag mismatch(es) — possible backup poisoning:"
                )
                for diff in result.diffs:
                    if diff.issue == "etag_mismatch":
                        typer.echo(
                            f"      - {diff.key}  "
                            f"source={diff.source_etag}  backup={diff.backup_etag}"
                        )

        if result.errors:
            any_failure = True
            for err in result.errors:
                typer.echo(f"    ✗ Error: {err}", err=True)

        typer.echo("")

    # ------------------------------------------------------------------
    # DynamoDB tables
    # ------------------------------------------------------------------
    if source_config.dynamodb_tables:
        dynamo_session = source_session or session
        dynamodb_client = dynamo_session.client("dynamodb")

        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            typer.echo(f"  DynamoDB: {table_name}")

            # Check PITR status
            try:
                resp = dynamodb_client.describe_continuous_backups(TableName=table_name)
                pitr = resp["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
                pitr_status = pitr.get("PointInTimeRecoveryStatus", "DISABLED")
                latest = pitr.get("LatestRestorableDateTime")
                ts = f"  [latest: {_fmt_dt(latest)}]" if latest else ""
                if pitr_status == "ENABLED":
                    typer.echo(f"    ✓ PITR enabled{ts}")
                else:
                    typer.echo("    ✗ PITR DISABLED — table cannot be restored to point-in-time")
                    any_failure = True
            except Exception as e:
                typer.echo(f"    ✗ Could not check PITR: {e}", err=True)
                any_failure = True

            # Check recent exports — paginate to get the real count
            try:
                exports: list[dict] = []
                kwargs: dict = {"TableArn": table_arn}
                while True:
                    resp = dynamodb_client.list_exports(**kwargs)
                    exports.extend(resp.get("ExportSummaries", []))
                    next_token = resp.get("NextToken")
                    if not next_token:
                        break
                    kwargs["NextToken"] = next_token
                completed = [e for e in exports if e.get("ExportStatus") == "COMPLETED"]
                if completed:
                    latest_export = max(completed, key=lambda e: e.get("ExportTime") or "")
                    export_ts = latest_export.get("ExportTime")
                    ts = f"  [latest: {_fmt_dt(export_ts)}]" if export_ts else ""
                    typer.echo(f"    ✓ {len(completed)} completed export(s) found{ts}")
                else:
                    typer.echo("    ✗ no completed exports found — export backup is missing")
                    any_failure = True
            except Exception as e:
                typer.echo(f"    ✗ Could not check exports: {e}", err=True)
                any_failure = True

            typer.echo("")

    if any_failure:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Programmatic restore-test API (used by daily health report Lambda)
# ---------------------------------------------------------------------------


def _sample_for_restore(
    session: boto3.Session,
    s3_client,
    source_config,
    source_alias: str,
    backup_bucket: str,
    sample_size: int,
) -> tuple[list[dict], int, str | None]:
    """Sample up to ``sample_size`` keys from a backup bucket.

    Returns ``(sample, archived_skipped, error_message)``. If error_message
    is set, sample is empty and caller should mark the bucket failed.
    """
    use_inventory = source_config.batch_manifest_mode == "inventory"
    if use_inventory:
        from nzshm_backup.athena_inventory import sample_objects_via_inventory

        try:
            sample = sample_objects_via_inventory(
                session, source_alias, backup_bucket, sample_size=sample_size
            )
            return sample, 0, None
        except Exception as e:
            return [], 0, f"Inventory unavailable for {backup_bucket}: {e}"

    # Bucket-listing path
    all_objects: list[dict] = []
    archived = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=backup_bucket):
        for obj in page.get("Contents", []):
            if any(obj["Key"].startswith(p) for p in OPERATIONAL_PREFIXES):
                continue
            if obj.get("StorageClass") in _ARCHIVED_STORAGE_CLASSES:
                archived += 1
            else:
                all_objects.append(obj)

    sample = (
        random.sample(all_objects, sample_size)
        if len(all_objects) >= sample_size
        else all_objects
    )
    return sample, archived, None


def _run_bucket_restore_test(
    session: boto3.Session,
    s3_client,
    source_config,
    source_alias: str,
    bucket_cfg,
    region: str,
    account_id: str,
    source_account_id: str,
    sample_size: int,
    use_batch: bool,
    batch_role_arn: str | None,
) -> BucketRestoreResult:
    """Execute the sample-and-verify path for one backup bucket.

    Side-effect-free with respect to stdout/stderr — captures all
    diagnostics in the returned ``BucketRestoreResult``. The CLI is
    expected to render the result for human display.
    """
    source_bucket = bucket_cfg.arn.split(":::")[-1]
    backup_bucket = source_config.get_backup_bucket_name(
        bucket_cfg.label, region, source_account_id, source_alias
    )
    sample, _archived, sample_err = _sample_for_restore(
        session, s3_client, source_config, source_alias, backup_bucket, sample_size
    )

    if sample_err:
        return BucketRestoreResult(
            source_bucket=source_bucket,
            backup_bucket=backup_bucket,
            result="failed",
            sample_count=0,
            copy_errors=[sample_err],
        )
    if not sample:
        # Empty sample treated as a failure: an empty backup bucket may
        # indicate data loss (drained bucket) rather than a never-populated one.
        return BucketRestoreResult(
            source_bucket=source_bucket,
            backup_bucket=backup_bucket,
            result="failed",
            sample_count=0,
            copy_errors=["No copyable objects found in backup bucket"],
        )

    ts = int(datetime.now(timezone.utc).timestamp())
    temp_bucket = f"bb-restore-test-{ts}-{account_id}"
    try:
        kwargs: dict = {"Bucket": temp_bucket, "ACL": "private"}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3_client.create_bucket(**kwargs)
        s3_client.put_bucket_tagging(
            Bucket=temp_bucket,
            Tagging={
                "TagSet": [
                    {"Key": "ManagedBy", "Value": "nzshm-backup"},
                    {"Key": "Type", "Value": "restore-test"},
                ]
            },
        )
        if use_batch and batch_role_arn:
            from nzshm_backup.s3_restore import apply_restore_target_policy

            apply_restore_target_policy(s3_client, temp_bucket, batch_role_arn)
    except Exception as e:
        return BucketRestoreResult(
            source_bucket=source_bucket,
            backup_bucket=backup_bucket,
            result="failed",
            sample_count=0,
            copy_errors=[f"Failed to create temp bucket: {e}"],
        )

    copy_errors: list[str] = []
    etag_mismatches: list[str] = []
    sampled_keys = [obj["Key"] for obj in sample]
    try:
        if use_batch:
            assert batch_role_arn

            def _sample_rows(_s: list = sample, _b: str = backup_bucket) -> Iterator[str]:
                for obj in _s:
                    safe_key = obj["Key"].replace('"', '""')
                    yield f"{_b},{safe_key}\n"

            from nzshm_backup.s3_batch import write_manifest_to_s3 as _write_manifest

            manifest_key = (
                f"_manifests/test-restore-{int(datetime.now(timezone.utc).timestamp())}.csv"
            )
            manifest_etag, manifest_row_count = _write_manifest(
                s3_client, _sample_rows(), backup_bucket, manifest_key
            )
            batch_result = batch_restore_bucket(
                session=session,
                backup_bucket=backup_bucket,
                target_bucket=temp_bucket,
                batch_role_arn=batch_role_arn,
                account_id=account_id,
                prebuilt_manifest_key=manifest_key,
                prebuilt_manifest_etag=manifest_etag,
                prebuilt_manifest_row_count=manifest_row_count,
            )
            if batch_result.status != "SUBMITTED":
                copy_errors.append(f"Batch job failed: {batch_result.errors}")
            else:
                assert batch_result.job_id
                try:
                    final_status = wait_for_batch_job(
                        session, account_id, batch_result.job_id, poll_interval=10, timeout=300
                    )
                    if final_status != "Complete":
                        copy_errors.append(f"Batch job ended with status: {final_status}")
                    else:
                        for obj in sample:
                            key = obj["Key"]
                            try:
                                err = _verify_restored_object(
                                    s3_client, backup_bucket, temp_bucket, key, obj["ETag"]
                                )
                                if err:
                                    etag_mismatches.append(f"{key}: {err}")
                            except Exception as e:
                                copy_errors.append(f"{key}: {e}")
                except TimeoutError as e:
                    copy_errors.append(str(e))
        else:
            for obj in sample:
                key = obj["Key"]
                expected_etag = obj["ETag"]
                try:
                    s3_client.copy_object(
                        CopySource={"Bucket": backup_bucket, "Key": key},
                        Bucket=temp_bucket,
                        Key=key,
                        MetadataDirective="COPY",
                    )
                    err = _verify_restored_object(
                        s3_client, backup_bucket, temp_bucket, key, expected_etag
                    )
                    if err:
                        etag_mismatches.append(f"{key}: {err}")
                except Exception as copy_err:
                    copy_errors.append(f"{key}: {copy_err}")
    finally:
        cleanup_err = _delete_temp_bucket_silent(s3_client, temp_bucket)
        if cleanup_err:
            copy_errors.append(cleanup_err)

    if copy_errors:
        result: Literal["passed", "failed", "skipped"] = "failed"
    elif etag_mismatches:
        result = "failed"
    else:
        result = "passed"

    return BucketRestoreResult(
        source_bucket=source_bucket,
        backup_bucket=backup_bucket,
        result=result,
        sample_count=len(sample),
        sampled_keys=sampled_keys,
        copy_errors=copy_errors,
        etag_mismatches=etag_mismatches,
    )


def restore_test_source(
    session: boto3.Session,
    config,
    source_alias: str,
    sample_size: int = 10,
    use_batch: bool = False,
    emit_events: bool = True,
) -> RestoreTestResult:
    """Run the S3 sample-restore verification for one source.

    Pure data-collection function: no stdout/stderr, no ``typer.Exit``.
    The CLI ``backup test restore`` and the daily health-report Lambda
    both call this and format the returned ``RestoreTestResult``.

    Args:
        emit_events: append a ``test_restore`` row per bucket to the
            event log. Defaults True for parity with the existing CLI;
            health-report callers can pass False to keep their own
            audit trail.
    """
    source_config = config.sources[source_alias]
    region = config.general.region
    account_id = get_account_id(session)
    source_account_id = source_config.source_account_id or account_id
    s3_client = session.client("s3")
    batch_role_arn = config.general.s3_batch_role_arn
    mode: Literal["direct copy", "batch"] = "batch" if use_batch else "direct copy"

    started = time.monotonic()
    result = RestoreTestResult(source=source_alias, mode=mode)
    for bucket_cfg in source_config.s3_buckets:
        bucket_result = _run_bucket_restore_test(
            session=session,
            s3_client=s3_client,
            source_config=source_config,
            source_alias=source_alias,
            bucket_cfg=bucket_cfg,
            region=region,
            account_id=account_id,
            source_account_id=source_account_id,
            sample_size=sample_size,
            use_batch=use_batch,
            batch_role_arn=batch_role_arn,
        )
        result.buckets.append(bucket_result)

        if emit_events:
            event_result = (
                "passed"
                if bucket_result.result == "passed"
                else "etag_mismatch"
                if not bucket_result.copy_errors and bucket_result.etag_mismatches
                else "failed"
            )
            details: dict = {
                "bucket": bucket_result.backup_bucket,
                "result": event_result,
                "mode": mode,
                "sample_size": bucket_result.sample_count,
            }
            if bucket_result.copy_errors:
                details["copy_errors"] = len(bucket_result.copy_errors)
            if bucket_result.etag_mismatches:
                details["etag_mismatches"] = len(bucket_result.etag_mismatches)
            append_event(
                session, bucket_result.backup_bucket, "test_restore", source_alias, details=details
            )

    result.duration_seconds = time.monotonic() - started
    return result


@app.command("restore")
def test_restore(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    sample_size: int = typer.Option(
        10, "--sample-size", help="Number of objects to sample from the backup bucket"
    ),
    use_batch: bool = typer.Option(
        False,
        "--use-batch",
        help="Exercise the S3 Batch Operations restore path instead of direct copy. "
        "Requires general.s3_batch_role_arn in config. Slower (Batch has per-job "
        "setup overhead) but validates the full production restore code path and IAM.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
):
    """Verify backup restorability without triggering a full restore.

    S3: copies a sample of objects from the backup bucket to a temporary bucket,
    verifies ETags match, then deletes the temp bucket. Proves the restore path
    works end-to-end and data is readable from backup.

    By default uses direct copy_object (fast, no IAM role required).
    Use --use-batch to exercise the S3 Batch Operations path instead.

    DynamoDB: confirms PITR is enabled on each table (a prerequisite for
    point-in-time restore) and checks the export bucket has accessible data.
    DynamoDB checks are read-only and run even with --dry-run.

    Objects in archived storage tiers (Glacier, Deep Archive) are skipped —
    they require a separate restore request before they can be copied.

    Exits with code 1 if any check fails.
    """
    state = get_state()
    if dry_run:
        state.dry_run = True

    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    source_config = config.sources[source]
    region = config.general.region
    session = boto3.Session()
    account_id = get_account_id(session)
    source_account_id = source_config.source_account_id or account_id
    s3 = session.client("s3")

    batch_role_arn: str | None = config.general.s3_batch_role_arn
    if use_batch and not batch_role_arn:
        typer.echo("Error: --use-batch requires general.s3_batch_role_arn in config.", err=True)
        raise typer.Exit(1)

    any_failure = False
    mode = "batch" if use_batch else "direct copy"
    typer.echo(f"\n[{source}] Restore test  (sample_size={sample_size}, mode={mode})\n")

    if state.dry_run:
        for bucket_cfg in source_config.s3_buckets:
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_cfg.label, region, source_account_id, source
            )
            typer.echo(f"  Sampling from: {backup_bucket}")
            typer.echo(f"    [DRY RUN] Would copy up to {sample_size} objects and verify ETags")
            typer.echo("")
    else:
        result = restore_test_source(
            session=session,
            config=config,
            source_alias=source,
            sample_size=sample_size,
            use_batch=use_batch,
        )
        for bucket_result in result.buckets:
            typer.echo(f"  Sampling from: {bucket_result.backup_bucket}")
            if bucket_result.result == "skipped":
                typer.echo(
                    f"    ⚠ {bucket_result.note or 'skipped'}",
                    err=True,
                )
            elif bucket_result.copy_errors:
                typer.echo(
                    f"    ✗ {len(bucket_result.copy_errors)} copy error(s):", err=True
                )
                for err in bucket_result.copy_errors:
                    typer.echo(f"      - {err}", err=True)
                any_failure = True
            elif bucket_result.etag_mismatches:
                typer.echo(
                    f"    ✗ {len(bucket_result.etag_mismatches)} ETag mismatch(es) after copy:",
                    err=True,
                )
                for key in bucket_result.etag_mismatches:
                    typer.echo(f"      - {key}", err=True)
                any_failure = True
            else:
                typer.echo(
                    f"    ✓ {bucket_result.sample_count} objects copied and verified"
                )
            typer.echo("")

    # ------------------------------------------------------------------
    # DynamoDB tables — check PITR enabled + export bucket accessible
    # ------------------------------------------------------------------
    if source_config.dynamodb_tables:
        if state.dry_run:
            typer.echo("  DynamoDB checks are read-only — running even in dry-run mode\n")
        source_session = (
            get_cross_account_session(session, source_config.source_account_role_arn)
            if source_config.source_account_role_arn
            else None
        )
        dynamo_session = source_session or session
        dynamodb_client = dynamo_session.client("dynamodb")

        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            typer.echo(f"  DynamoDB restorability: {table_name}")

            # PITR check — confirms point-in-time restore is available
            try:
                resp = dynamodb_client.describe_continuous_backups(TableName=table_name)
                pitr = resp["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
                if pitr.get("PointInTimeRecoveryStatus") == "ENABLED":
                    latest_dt = pitr.get("LatestRestorableDateTime")
                    pitr_ts = f"  [latest: {_fmt_dt(latest_dt)}]" if latest_dt else ""
                    typer.echo(f"    ✓ PITR enabled{pitr_ts}")
                else:
                    typer.echo("    ✗ PITR DISABLED — point-in-time restore unavailable")
                    any_failure = True
            except Exception as e:
                typer.echo(f"    ✗ Could not check PITR: {e}", err=True)
                any_failure = True

            # Export bucket spot-check — confirms export data is accessible
            export_bucket = source_config.get_dynamodb_backup_bucket_name(
                source, config.general.region, source_account_id
            )
            try:
                resp = s3.list_objects_v2(Bucket=export_bucket, MaxKeys=1)
                count = resp.get("KeyCount", 0)
                if count > 0:
                    typer.echo(f"    ✓ export bucket accessible: {export_bucket}")
                else:
                    typer.echo(f"    ✗ export bucket {export_bucket} is empty — no export data")
                    any_failure = True
            except Exception as e:
                typer.echo(f"    ✗ export bucket {export_bucket} not accessible: {e}", err=True)
                any_failure = True

            typer.echo("")

    if any_failure:
        raise typer.Exit(1)


def _delete_temp_bucket_silent(s3_client, bucket_name: str) -> str | None:
    """Delete all objects then the bucket. Returns error message or None.

    Used by both the CLI (which echoes the error) and the programmatic
    ``restore_test_source`` (which captures it in the result).
    """
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3_client.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})
        s3_client.delete_bucket(Bucket=bucket_name)
        return None
    except Exception as e:
        return f"failed to clean up temp bucket {bucket_name}: {e}"


def _delete_temp_bucket(s3_client, bucket_name: str) -> None:
    """Delete all objects then the bucket. Failures are logged, not raised."""
    err = _delete_temp_bucket_silent(s3_client, bucket_name)
    if err:
        typer.echo(f"    Warning: {err}", err=True)


@app.command("full-drill")
def test_full_drill(
    source: str = typer.Option(..., help="Data source to test"),
    isolated_environment: bool = typer.Option(False, help="Restore to isolated environment"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without executing"
    ),
):
    """Run quarterly full disaster recovery drill."""
    typer.echo(f"Full DR drill - coming soon for {source}")


@app.command("alert")
def test_alert(
    stage: str = typer.Option("prod", help="Deployment stage matching `sls deploy --stage`"),
    region: str = typer.Option("ap-southeast-2", help="AWS region of the alarm"),
) -> None:
    """Force the Lambda-error alarm into ALARM state to test the fast-path notification.

    Fires the alarm's SNS actions (email/Slack subscribers) without requiring the
    backup Lambda to actually fail. The alarm auto-returns to OK on the next real
    metric datapoint (~5 min). See ADR-005 / docs/design/adr/ADR-005-*.md.
    """
    alarm_name = f"nzshm-backup-lambda-errors-{stage}"
    typer.echo(f"Forcing alarm to ALARM state: {alarm_name}  (region={region})")

    cw = boto3.client("cloudwatch", region_name=region)
    cw.set_alarm_state(
        AlarmName=alarm_name,
        StateValue="ALARM",
        StateReason="Manual test via `backup test alert`",
    )

    typer.echo("  ✓ Alarm state set to ALARM.")
    typer.echo("  → SNS actions fired; subscribed recipients should receive notification")
    typer.echo("    within ~30 seconds.")
    typer.echo("  → Alarm auto-returns to OK on the next real datapoint (~5 min).")
    typer.echo("    An OK notification will also be delivered (OKActions wired).")
