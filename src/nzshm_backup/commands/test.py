"""Testing and validation commands."""

import random
from datetime import datetime, timezone

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.integrity import OPERATIONAL_PREFIXES, check_bucket_integrity
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session

app = typer.Typer()

_ARCHIVED_STORAGE_CLASSES = {"GLACIER", "GLACIER_IR", "DEEP_ARCHIVE"}


@app.command("integrity")
def test_integrity(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
):
    """Validate backup integrity by comparing source ↔ backup object counts and ETags.

    Reports missing objects (in source but not in backup) and ETag mismatches
    (possible backup poisoning — source mutation propagated to backup).

    Exits with code 1 if any discrepancies are found.
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

    for bucket_cfg in source_config.s3_buckets:
        source_bucket = bucket_cfg.arn.split(":::")[-1]
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )

        typer.echo(f"  {source_bucket}  ↔  {backup_bucket}")
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
                typer.echo(f"    ✗ {result.mismatch_count} ETag mismatch(es) — possible backup poisoning:")
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

    if any_failure:
        raise typer.Exit(1)


@app.command("restore")
def test_restore(
    source: str = typer.Option(..., "--source", help="Source alias from config"),
    sample_size: int = typer.Option(
        10, "--sample-size", help="Number of objects to sample from the backup bucket"
    ),
):
    """Verify backup restorability by copying a sample of objects to a temp bucket.

    Picks up to --sample-size objects from the backup bucket, copies them to a
    temporary bucket, verifies ETags match, then deletes the temp bucket.

    Objects in archived storage tiers (Glacier, Deep Archive) are skipped —
    they require a separate restore request before they can be copied.

    Exits with code 1 if any copy fails or ETag mismatches are found.
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
    s3 = session.client("s3")

    any_failure = False
    typer.echo(f"\n[{source}] Restore test  (sample_size={sample_size})\n")

    for bucket_cfg in source_config.s3_buckets:
        backup_bucket = source_config.get_backup_bucket_name(
            bucket_cfg.label, region, source_account_id, source
        )
        typer.echo(f"  Sampling from: {backup_bucket}")

        # Collect copyable objects
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

        if not all_objects:
            typer.echo("    ✗ No copyable objects found in backup bucket", err=True)
            any_failure = True
            continue

        sample = (
            random.sample(all_objects, sample_size)
            if len(all_objects) >= sample_size
            else all_objects
        )
        if len(sample) < sample_size:
            typer.echo(f"    Sample reduced to {len(sample)} (fewer copyable objects than requested)")

        # Create temp bucket
        ts = int(datetime.now(timezone.utc).timestamp())
        temp_bucket = f"bb-restore-test-{ts}-{account_id}"
        typer.echo(f"    Creating temp bucket: {temp_bucket}")
        try:
            kwargs: dict = {"Bucket": temp_bucket, "ACL": "private"}
            if region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**kwargs)
            s3.put_bucket_tagging(
                Bucket=temp_bucket,
                Tagging={"TagSet": [{"Key": "ManagedBy", "Value": "nzshm-backup"}, {"Key": "Type", "Value": "restore-test"}]},
            )
        except Exception as e:
            typer.echo(f"    ✗ Failed to create temp bucket: {e}", err=True)
            any_failure = True
            continue

        # Copy and verify
        copy_errors: list[str] = []
        etag_mismatches: list[str] = []
        try:
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
        elif etag_mismatches:
            typer.echo(f"    ✗ {len(etag_mismatches)} ETag mismatch(es) after copy:", err=True)
            for key in etag_mismatches:
                typer.echo(f"      - {key}", err=True)
            any_failure = True
        else:
            typer.echo(f"    ✓ {len(sample)} objects copied and verified")

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
                s3_client.delete_objects(
                    Bucket=bucket_name, Delete={"Objects": objects}
                )
        s3_client.delete_bucket(Bucket=bucket_name)
    except Exception as e:
        typer.echo(f"    Warning: failed to clean up temp bucket {bucket_name}: {e}", err=True)


@app.command("full-drill")
def test_full_drill(
    source: str = typer.Option(..., help="Data source to test"),
    isolated_environment: bool = typer.Option(False, help="Restore to isolated environment"),
):
    """Run quarterly full disaster recovery drill."""
    typer.echo(f"Full DR drill - coming soon for {source}")
