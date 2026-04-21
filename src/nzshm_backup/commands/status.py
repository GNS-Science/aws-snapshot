"""Status command - current backup state."""

import json
from datetime import datetime
from typing import Any, Literal

import boto3
import typer

from nzshm_backup.config import load_config
from nzshm_backup.inventory_state import inventory_health_for_bucket_pair
from nzshm_backup.run_state import read_run_state
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session
from nzshm_backup.s3_batch import list_recent_batch_jobs


def _fmt_dt(dt: datetime | str) -> str:
    """Format datetime (or ISO string) in local timezone."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


app = typer.Typer()

EXPORT_LIMIT = 5  # most recent exports to show per table
BATCH_JOB_LIMIT = 3  # most recent batch jobs to show per bucket

BATCH_STATUS_ICON = {
    "Complete": "✓",
    "Failed": "✗",
    "Cancelled": "✗",
    "Active": "⋯",
    "Completing": "⋯",
    "Preparing": "⋯",
    "New": "⋯",
    "Paused": "⏸",
    "Suspended": "⏸",
}


def _progress_fields(job: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized progress fields from a batch job dict."""
    progress = job.get("ProgressSummary", {})
    total = int(progress.get("TotalNumberOfTasks", 0) or 0)
    failed = int(progress.get("NumberOfTasksFailed", 0) or 0)
    succeeded = int(progress.get("NumberOfTasksSucceeded", 0) or 0)
    status = job.get("Status", "Unknown")

    # FailedTasksOnly report may not include succeeded count.
    if status == "Complete" and failed == 0 and total > 0:
        succeeded = total

    done = succeeded + failed
    percent_complete = round((done / total) * 100, 1) if total else 0.0
    return {
        "total_tasks": total,
        "tasks_succeeded": succeeded,
        "tasks_failed": failed,
        "percent_complete": percent_complete,
    }


def _job_json(job: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-serializable batch job summary."""
    progress = _progress_fields(job)
    return {
        "job_id": job.get("JobId", ""),
        "status": job.get("Status", "Unknown"),
        "description": job.get("Description", ""),
        "creation_time": str(job.get("CreationTime", "")),
        **progress,
    }


def _print_selected_job(job: dict[str, Any]) -> None:
    """Print detailed status for a specifically requested batch job."""
    status = job.get("Status", "Unknown")
    icon = BATCH_STATUS_ICON.get(status, "?")
    job_id = str(job.get("JobId", ""))
    created = job.get("CreationTime")
    ts = f"  [{_fmt_dt(created)}]" if created else ""
    progress = _progress_fields(job)
    typer.echo("  Selected S3 Batch job:")
    typer.echo(f"    {icon} job/{job_id}: {status}{ts}")
    typer.echo(
        "      progress: "
        f"{progress['tasks_succeeded']} succeeded, "
        f"{progress['tasks_failed']} failed, "
        f"{progress['total_tasks']} total "
        f"({progress['percent_complete']}% done)"
    )


def _get_recent_batch_jobs(s3control_client, account_id: str, source_bucket: str) -> list[dict]:
    return list_recent_batch_jobs(
        s3control_client, account_id, source_bucket, limit=BATCH_JOB_LIMIT
    )


def _get_recent_exports(dynamodb_client, table_arn: str, limit: int = EXPORT_LIMIT) -> list[dict]:
    """Return the most recent exports for a table, newest first."""
    exports = []
    kwargs: dict = {"TableArn": table_arn, "MaxResults": 25}
    while True:
        response = dynamodb_client.list_exports(**kwargs)
        exports.extend(response.get("ExportSummaries", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    exports.sort(key=lambda e: e.get("ExportTime") or "", reverse=True)
    return exports[:limit]


def _print_source_status(
    source_alias: str,
    source_config,
    session: boto3.Session,
    config,
    selected_job: dict[str, Any] | None = None,
) -> None:
    source_session = (
        get_cross_account_session(session, source_config.source_account_role_arn)
        if source_config.source_account_role_arn
        else session
    )
    dynamodb_client = source_session.client("dynamodb")

    typer.echo(f"\n[{source_alias}] {source_config.display_name}")

    if not source_config.dynamodb_tables:
        typer.echo("  DynamoDB: no tables configured")
    else:
        typer.echo("  DynamoDB exports:")
        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            try:
                exports = _get_recent_exports(dynamodb_client, table_arn)
                if not exports:
                    typer.echo(f"    {table_name}: no exports found")
                    continue
                latest = exports[0]
                status = latest["ExportStatus"]
                status_icon = {"COMPLETED": "✓", "IN_PROGRESS": "⋯", "FAILED": "✗"}.get(status, "?")

                detail = dynamodb_client.describe_export(ExportArn=latest["ExportArn"])
                desc = detail["ExportDescription"]
                start_time = desc.get("StartTime")
                ts = f"  [{_fmt_dt(start_time)}]" if start_time else ""
                typer.echo(f"    {status_icon} {table_name}: {status}{ts}")

                if status == "FAILED":
                    msg = desc.get("FailureMessage", "")
                    if "because" in msg:
                        msg = msg.split("because")[-1].strip()
                    typer.echo(f"      reason: {msg[:120]}")
            except Exception as e:
                typer.echo(f"    {table_name}: error fetching status ({e})")

    if not source_config.s3_buckets:
        typer.echo("  S3: no buckets configured")
    elif source_config.use_s3_batch:
        typer.echo("  S3 buckets (batch mode):")
        account_id = get_account_id(session)
        s3control = session.client("s3control", region_name=config.general.region)
        if selected_job is not None:
            _print_selected_job(selected_job)
        for bucket_config in source_config.s3_buckets:
            source_bucket = bucket_config.arn.split(":::")[-1]
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_config.label,
                config.general.region,
                source_config.source_account_id or get_account_id(session),
                source_alias,
            )
            try:
                state = read_run_state(session, backup_bucket)
                if state:
                    checked = _fmt_dt(state["checked_at"]) if state.get("checked_at") else "unknown"
                    st = state.get("status", "unknown")
                    if st == "running":
                        typer.echo(
                            "    last run: "
                            f"{checked} — running "
                            "(preparing manifest; batch job not submitted yet)"
                        )
                    elif st == "prepared":
                        typer.echo(
                            "    last run: "
                            f"{checked} — prepared "
                            "(manifest ready; batch job intentionally not submitted)"
                        )
                    else:
                        typer.echo(f"    last run: {checked} — {st}")

                inv = inventory_health_for_bucket_pair(
                    session,
                    source_session,
                    source_alias,
                    source_bucket,
                    backup_bucket,
                )
                src_ts = inv.get("source_latest")
                bkp_ts = inv.get("backup_latest")
                eff_ts = inv.get("effective_data_ts")
                if src_ts or bkp_ts:
                    src_s = _fmt_dt(src_ts) if src_ts else "none"
                    bkp_s = _fmt_dt(bkp_ts) if bkp_ts else "none"
                    eff_s = _fmt_dt(eff_ts) if eff_ts else "none"
                    typer.echo(f"    inventory: source={src_s}  backup={bkp_s}  effective={eff_s}")

                jobs = _get_recent_batch_jobs(s3control, account_id, source_bucket)
                if not jobs:
                    typer.echo(f"    {source_bucket}: no batch jobs found")
                    continue
                for job in jobs:
                    status = job.get("Status", "Unknown")
                    icon = BATCH_STATUS_ICON.get(status, "?")
                    job_id = job.get("JobId", "")[:8]
                    created = job.get("CreationTime")
                    ts = f"  [{_fmt_dt(created)}]" if created else ""
                    progress = _progress_fields(job)
                    total = progress["total_tasks"]
                    failed = progress["tasks_failed"]
                    succeeded = progress["tasks_succeeded"]
                    if failed:
                        progress_str = f"{failed} failed / {total} objects"
                    else:
                        progress_str = f"{succeeded}/{total} objects"
                    typer.echo(
                        f"    {icon} {source_bucket}: {status}  job/{job_id}…{ts}  ({progress_str})"
                    )
            except Exception as e:
                typer.echo(f"    {source_bucket}: error fetching batch status ({e})")
    else:
        typer.echo(f"  S3 buckets (incremental): {len(source_config.s3_buckets)} configured")
        for bucket_config in source_config.s3_buckets:
            backup_bucket = source_config.get_backup_bucket_name(
                bucket_config.label,
                config.general.region,
                source_config.source_account_id or get_account_id(session),
                source_alias,
            )
            state = read_run_state(session, backup_bucket)
            if state:
                checked = _fmt_dt(state["checked_at"]) if state.get("checked_at") else "unknown"
                st = state.get("status", "unknown")
                detail = ""
                if st == "completed":
                    detail = f"  {state.get('objects_copied', 0)} objects copied"
                elif st == "submitted":
                    job_id = state.get("batch_job_id", "")[:8]
                    n_obj = state.get("objects_in_manifest", 0)
                    detail = f"  job/{job_id}…  {n_obj} objects"
                typer.echo(f"    last run: {checked} — {st}{detail}")


@app.callback(invoke_without_command=True)
def status(
    source: str = typer.Option("all", help="Source name from config, or 'all'"),
    output: Literal["text", "json"] = typer.Option("text", help="Output format"),
    job_id: str | None = typer.Option(
        None,
        "--job-id",
        help="Inspect a specific S3 Batch job ID (requires --source with batch-enabled source)",
    ),
):
    """Show current backup status.

    Displays recent DynamoDB export status per table for each source.
    Cross-account sources are queried transparently via the configured reader role.
    """
    try:
        config = load_config()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if source != "all" and source not in config.sources:
        valid = ", ".join(sorted(config.sources.keys()))
        typer.echo(f"Error: unknown source '{source}'. Valid: {valid}", err=True)
        raise typer.Exit(1)

    if job_id and source == "all":
        typer.echo("Error: --job-id requires --source <alias>", err=True)
        raise typer.Exit(1)

    if job_id and source != "all" and not config.sources[source].use_s3_batch:
        typer.echo(f"Error: source '{source}' is not configured for S3 Batch", err=True)
        raise typer.Exit(1)

    sources_to_check = list(config.sources.keys()) if source == "all" else [source]

    session = boto3.Session()
    selected_job: dict[str, Any] | None = None
    if job_id:
        account_id = get_account_id(session)
        s3control = session.client("s3control", region_name=config.general.region)
        try:
            selected_job = s3control.describe_job(AccountId=account_id, JobId=job_id)["Job"]
        except Exception as e:
            typer.echo(f"Error: failed to fetch S3 Batch job '{job_id}' ({e})", err=True)
            raise typer.Exit(1) from e

    if output == "json":
        _print_json_status(sources_to_check, config, session, selected_job)
        return

    for alias in sources_to_check:
        _print_source_status(alias, config.sources[alias], session, config, selected_job)
    typer.echo("")


def _print_json_status(
    sources: list[str],
    config,
    session: boto3.Session,
    selected_job: dict[str, Any] | None = None,
) -> None:
    out = {}
    account_id = get_account_id(session)
    s3control = session.client("s3control", region_name=config.general.region)
    for alias in sources:
        source_config = config.sources[alias]
        source_session = (
            get_cross_account_session(session, source_config.source_account_role_arn)
            if source_config.source_account_role_arn
            else session
        )
        dynamodb_client = source_session.client("dynamodb")
        tables: dict[str, Any] = {}
        for table_arn in source_config.dynamodb_tables:
            table_name = table_arn.split("/")[-1]
            try:
                exports = _get_recent_exports(dynamodb_client, table_arn)
                tables[table_name] = [
                    {
                        "export_arn": e["ExportArn"],
                        "status": e["ExportStatus"],
                        "export_time": str(e.get("ExportTime", "")),
                    }
                    for e in exports
                ]
            except Exception as e:
                tables[table_name] = {"error": str(e)}

        s3_batches: list[dict[str, Any]] = []
        if source_config.use_s3_batch:
            for bucket_config in source_config.s3_buckets:
                source_bucket = bucket_config.arn.split(":::")[-1]
                backup_bucket = source_config.get_backup_bucket_name(
                    bucket_config.label,
                    config.general.region,
                    source_config.source_account_id or account_id,
                    alias,
                )
                try:
                    state = read_run_state(session, backup_bucket)
                    inv = inventory_health_for_bucket_pair(
                        session,
                        source_session,
                        alias,
                        source_bucket,
                        backup_bucket,
                    )
                    jobs = _get_recent_batch_jobs(s3control, account_id, source_bucket)
                    s3_batches.append(
                        {
                            "source_bucket": source_bucket,
                            "backup_bucket": backup_bucket,
                            "last_run": state,
                            "inventory": {
                                "source_latest": str(inv.get("source_latest") or ""),
                                "backup_latest": str(inv.get("backup_latest") or ""),
                                "effective_data_ts": str(inv.get("effective_data_ts") or ""),
                                "source_configured": inv.get("source_configured", False),
                                "backup_configured": inv.get("backup_configured", False),
                            },
                            "recent_jobs": [_job_json(j) for j in jobs],
                        }
                    )
                except Exception as e:
                    s3_batches.append(
                        {
                            "source_bucket": source_bucket,
                            "backup_bucket": backup_bucket,
                            "error": str(e),
                        }
                    )

        payload: dict[str, Any] = {
            "dynamodb_tables": tables,
            "s3_batches": s3_batches,
        }
        if selected_job is not None and source_config.use_s3_batch:
            payload["selected_job"] = _job_json(selected_job)

        out[alias] = payload
    typer.echo(json.dumps(out, indent=2))
