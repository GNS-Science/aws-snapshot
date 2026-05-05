"""Testing and validation commands."""

import random
from collections.abc import Iterator
from datetime import datetime, timezone

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.event_log import append_event
from nzshm_backup.integrity import OPERATIONAL_PREFIXES, check_bucket_integrity
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.s3_batch import batch_restore_bucket, wait_for_batch_job
from nzshm_backup.state import get_state


def _fmt_dt(dt: datetime | str) -> str:
    """Format datetime (or ISO string) in local timezone."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


app = typer.Typer()

_ARCHIVED_STORAGE_CLASSES = {"GLACIER", "GLACIER_IR", "DEEP_ARCHIVE"}


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

    for bucket_cfg in source_config.s3_buckets:
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )
        typer.echo(f"  Sampling from: {backup_bucket}")

        # Sample objects — prefer inventory (fast) over full listing (slow)
        sample: list[dict] = []
        use_inventory = source_config.batch_manifest_mode == "inventory"
        if use_inventory:
            try:
                from nzshm_backup.athena_inventory import sample_objects_via_inventory

                typer.echo("    Sampling via inventory (Athena)...")
                sample = sample_objects_via_inventory(
                    session, source, backup_bucket, sample_size=sample_size,
                )
            except (ValueError, Exception) as e:
                typer.echo(f"    Inventory sampling failed ({e}), falling back to listing")
                use_inventory = False

        if not use_inventory:
            typer.echo("    Sampling via bucket listing...")
            all_objects: list[dict] = []
            archived_count = 0
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=backup_bucket):
                for obj in page.get("Contents", []):
                    if any(obj["Key"].startswith(p) for p in OPERATIONAL_PREFIXES):
                        continue
                    if obj.get("StorageClass") in _ARCHIVED_STORAGE_CLASSES:
                        archived_count += 1
                    else:
                        all_objects.append(obj)

            if archived_count:
                typer.echo(
                    f"    {archived_count} archived object(s) skipped "
                    f"(Glacier/Deep Archive — not directly copyable)"
                )
            sample = (
                random.sample(all_objects, sample_size)
                if len(all_objects) >= sample_size
                else all_objects
            )

        if not sample:
            typer.echo("    ✗ No copyable objects found in backup bucket", err=True)
            any_failure = True
            continue

        if len(sample) < sample_size:
            typer.echo(
                f"    Sample reduced to {len(sample)} (fewer copyable objects than requested)"
            )

        # Create temp bucket
        ts = int(datetime.now(timezone.utc).timestamp())
        temp_bucket = f"bb-restore-test-{ts}-{account_id}"
        if state.dry_run:
            typer.echo(f"    [DRY RUN] Would create temp bucket: {temp_bucket}")
            typer.echo(f"    [DRY RUN] Would copy {len(sample)} objects and verify ETags")
            continue

        typer.echo(f"    Creating temp bucket: {temp_bucket}")
        try:
            kwargs: dict = {"Bucket": temp_bucket, "ACL": "private"}
            if region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**kwargs)
            s3.put_bucket_tagging(
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

                apply_restore_target_policy(s3, temp_bucket, batch_role_arn)
        except Exception as e:
            typer.echo(f"    ✗ Failed to create temp bucket: {e}", err=True)
            any_failure = True
            continue

        # Copy sample to temp bucket and verify ETags
        copy_errors: list[str] = []
        etag_mismatches: list[str] = []
        try:
            if use_batch:
                assert batch_role_arn  # guarded: use_batch requires batch_role_arn (checked above)

                # Write a manifest containing only the sampled keys, then submit a Batch job
                def _sample_rows(_s: list = sample, _b: str = backup_bucket) -> Iterator[str]:
                    for obj in _s:
                        safe_key = obj["Key"].replace('"', '""')
                        yield f"{_b},{safe_key}\n"

                from nzshm_backup.s3_batch import write_manifest_to_s3 as _write_manifest

                manifest_key = (
                    f"_manifests/test-restore-{int(datetime.now(timezone.utc).timestamp())}.csv"
                )
                manifest_etag, manifest_row_count = _write_manifest(
                    s3, _sample_rows(), backup_bucket, manifest_key
                )
                typer.echo(f"    Submitting batch job ({len(sample)} objects)...")
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
                    typer.echo(f"    Waiting for batch job {batch_result.job_id}...")
                    assert batch_result.job_id  # set when status == "SUBMITTED"
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
                                    head = s3.head_object(Bucket=temp_bucket, Key=key)
                                    if head["ETag"] != obj["ETag"]:
                                        etag_mismatches.append(key)
                                except Exception as e:
                                    copy_errors.append(f"{key}: {e}")
                    except TimeoutError as e:
                        copy_errors.append(str(e))
            else:
                for obj in sample:
                    key = obj["Key"]
                    expected_etag = obj["ETag"]
                    try:
                        s3.copy_object(
                            CopySource={"Bucket": backup_bucket, "Key": key},
                            Bucket=temp_bucket,
                            Key=key,
                            MetadataDirective="COPY",
                        )
                        head = s3.head_object(Bucket=temp_bucket, Key=key)
                        if head["ETag"] != expected_etag:
                            etag_mismatches.append(key)
                    except Exception as copy_err:
                        copy_errors.append(f"{key}: {copy_err}")
        finally:
            # Always clean up temp bucket
            _delete_temp_bucket(s3, temp_bucket)

        if copy_errors:
            typer.echo(f"    ✗ {len(copy_errors)} copy error(s):", err=True)
            for err in copy_errors:
                typer.echo(f"      - {err}", err=True)
            any_failure = True
            append_event(
                session,
                backup_bucket,
                "test_restore",
                source,
                details={
                    "bucket": backup_bucket,
                    "result": "failed",
                    "mode": mode,
                    "sample_size": len(sample),
                    "copy_errors": len(copy_errors),
                },
            )
        elif etag_mismatches:
            typer.echo(f"    ✗ {len(etag_mismatches)} ETag mismatch(es) after copy:", err=True)
            for key in etag_mismatches:
                typer.echo(f"      - {key}", err=True)
            any_failure = True
            append_event(
                session,
                backup_bucket,
                "test_restore",
                source,
                details={
                    "bucket": backup_bucket,
                    "result": "etag_mismatch",
                    "mode": mode,
                    "sample_size": len(sample),
                    "etag_mismatches": len(etag_mismatches),
                },
            )
        else:
            typer.echo(f"    ✓ {len(sample)} objects copied and verified")
            append_event(
                session,
                backup_bucket,
                "test_restore",
                source,
                details={
                    "bucket": backup_bucket,
                    "result": "passed",
                    "mode": mode,
                    "sample_size": len(sample),
                },
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


def _delete_temp_bucket(s3_client, bucket_name: str) -> None:
    """Delete all objects then the bucket. Failures are logged, not raised."""
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3_client.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})
        s3_client.delete_bucket(Bucket=bucket_name)
    except Exception as e:
        typer.echo(f"    Warning: failed to clean up temp bucket {bucket_name}: {e}", err=True)


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
