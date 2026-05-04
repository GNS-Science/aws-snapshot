"""Athena-backed helpers for inventory-based manifest preparation."""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import boto3

from nzshm_backup.inventory_state import _expected_prefix

logger = logging.getLogger(__name__)

_SYMLINK_DT_RE = re.compile(r"/hive/dt=([^/]+)/symlink\.txt$")


def _sanitize_identifier(value: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", value)
    ident = re.sub(r"_+", "_", ident).strip("_")
    return ident.lower() or "x"


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    without_scheme = uri[5:]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return bucket, key


def _latest_inventory_partition(
    s3_client,
    control_bucket: str,
    inventory_prefix: str,
) -> tuple[str, str]:
    dt_to_symlink: dict[str, str] = {}
    prefix = f"{inventory_prefix.rstrip('/')}/"
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=control_bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            match = _SYMLINK_DT_RE.search(key)
            if not match:
                continue
            dt_to_symlink[match.group(1)] = key

    if not dt_to_symlink:
        raise ValueError(f"No inventory hive partitions found under s3://{control_bucket}/{prefix}")

    dt = max(dt_to_symlink)
    symlink_key = dt_to_symlink[dt]
    hive_root = symlink_key.split("/hive/dt=")[0] + "/hive/"
    return dt, hive_root


def _table_name(source_alias: str, side: str, bucket: str) -> str:
    return _sanitize_identifier(f"inv_{source_alias}_{side}_{bucket}")


def _run_athena_query(
    athena_client,
    query: str,
    output_location: str,
    database: str | None = None,
) -> str:
    request: dict[str, Any] = {
        "QueryString": query,
        "ResultConfiguration": {"OutputLocation": output_location},
    }
    if database:
        request["QueryExecutionContext"] = {"Database": database}
    response = athena_client.start_query_execution(**request)
    return response["QueryExecutionId"]


def _wait_for_athena_query(
    athena_client,
    query_execution_id: str,
    timeout_seconds: int = 900,
) -> None:
    deadline = time.time() + timeout_seconds
    while True:
        execution = athena_client.get_query_execution(QueryExecutionId=query_execution_id)
        status = execution["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            return
        if state in {"FAILED", "CANCELLED"}:
            reason = status.get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {query_execution_id} {state}: {reason}")
        if time.time() >= deadline:
            raise TimeoutError(
                f"Athena query {query_execution_id} timed out after {timeout_seconds}s"
            )
        time.sleep(2)


def _ensure_inventory_table(
    athena_client,
    output_location: str,
    database: str,
    table_name: str,
    control_bucket: str,
    hive_root: str,
) -> None:
    create_db = f"CREATE DATABASE IF NOT EXISTS {database}"
    qid = _run_athena_query(athena_client, create_db, output_location)
    _wait_for_athena_query(athena_client, qid)

    location = f"s3://{control_bucket}/{hive_root}"
    create_table = f"""
CREATE EXTERNAL TABLE IF NOT EXISTS {table_name} (
  bucket string,
  key string,
  version_id string,
  is_latest boolean,
  is_delete_marker boolean,
  size bigint,
  last_modified_date timestamp,
  e_tag string,
  storage_class string
)
PARTITIONED BY (dt string)
ROW FORMAT SERDE 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe'
STORED AS INPUTFORMAT 'org.apache.hadoop.hive.ql.io.SymlinkTextInputFormat'
OUTPUTFORMAT 'org.apache.hadoop.hive.ql.io.IgnoreKeyTextOutputFormat'
LOCATION '{location}'
""".strip()
    qid = _run_athena_query(athena_client, create_table, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)


def _ensure_partition(
    athena_client,
    output_location: str,
    database: str,
    table_name: str,
    control_bucket: str,
    hive_root: str,
    dt: str,
) -> None:
    location = f"s3://{control_bucket}/{hive_root}dt={dt}/"
    query = (
        f"ALTER TABLE {table_name} ADD IF NOT EXISTS PARTITION (dt='{dt}') "
        f"LOCATION '{location}'"
    )
    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)


def _iter_query_result_keys(s3_client, result_location: str) -> Iterator[str]:
    bucket, key = _parse_s3_uri(result_location)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"]
    # Stream line-by-line to avoid loading the entire result into memory.
    first = True
    for raw_line in body.iter_lines():
        line = raw_line.decode("utf-8").strip()
        if not line:
            continue
        if first:
            first = False
            continue
        row = next(csv.reader([line]))
        if row and row[0]:
            yield row[0]


def build_inventory_manifest_rows_via_athena(
    session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
    full_sync: bool = False,
) -> tuple[Iterator[str], str, str, int]:
    s3_client = session.client("s3")
    athena_client = session.client("athena")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"

    source_prefix = _expected_prefix(source_alias, "source", source_bucket)
    backup_prefix = _expected_prefix(source_alias, "backup", backup_bucket)

    source_dt, source_hive_root = _latest_inventory_partition(
        s3_client,
        control_bucket,
        source_prefix,
    )

    try:
        backup_dt, backup_hive_root = _latest_inventory_partition(
            s3_client,
            control_bucket,
            backup_prefix,
        )
    except ValueError:
        # Backup bucket is empty (first-ever backup) — no inventory partitions.
        # Fall back to source-only query (copies everything).
        logger.info(
            "No backup inventory partitions for %s — treating as full sync (first backup)",
            backup_bucket,
        )
        backup_dt = None
        backup_hive_root = None

    database = "nzshm_backup_inventory"
    source_table = _table_name(source_alias, "source", source_bucket)
    backup_table = _table_name(source_alias, "backup", backup_bucket)
    output_location = f"s3://{control_bucket}/athena-results/{source_alias}/{source_bucket}/"

    _ensure_inventory_table(
        athena_client,
        output_location,
        database,
        source_table,
        control_bucket,
        source_hive_root,
    )
    if backup_hive_root is not None:
        _ensure_inventory_table(
            athena_client,
            output_location,
            database,
            backup_table,
            control_bucket,
            backup_hive_root,
        )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        source_table,
        control_bucket,
        source_hive_root,
        source_dt,
    )
    if backup_dt is not None:
        _ensure_partition(
            athena_client,
            output_location,
            database,
            backup_table,
            control_bucket,
            backup_hive_root,
            backup_dt,
        )

    # Non-versioned buckets have NULL is_latest/is_delete_marker in inventory,
    # so accept both true and NULL for is_latest, and both false and NULL for
    # is_delete_marker.
    _src_filter = (
        f"dt = '{source_dt}'"
        " AND (is_latest = true OR is_latest IS NULL)"
        " AND (is_delete_marker = false OR is_delete_marker IS NULL)"
    )
    _dst_filter = (
        f"dt = '{backup_dt}'"
        " AND (is_latest = true OR is_latest IS NULL)"
        " AND (is_delete_marker = false OR is_delete_marker IS NULL)"
        " AND key NOT LIKE '_manifests/%'"
        " AND key NOT LIKE '_batch-reports/%'"
        " AND key NOT LIKE '_state/%'"
    ) if backup_dt else ""

    if full_sync or backup_dt is None:
        query = f"""
SELECT key
FROM {source_table}
WHERE {_src_filter}
""".strip()
    else:
        query = f"""
WITH src AS (
  SELECT key, size, e_tag
  FROM {source_table}
  WHERE {_src_filter}
),
dst AS (
  SELECT key, size, e_tag
  FROM {backup_table}
  WHERE {_dst_filter}
)
SELECT s.key
FROM src s
LEFT JOIN dst d ON s.key = d.key
WHERE d.key IS NULL
   OR s.size <> d.size
   OR s.e_tag <> d.e_tag
""".strip()

    query_id = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, query_id)
    query_execution = athena_client.get_query_execution(QueryExecutionId=query_id)
    result_location = query_execution["QueryExecution"]["ResultConfiguration"]["OutputLocation"]

    # Get result file size for progress estimation
    result_bucket, result_key = _parse_s3_uri(result_location)
    try:
        head = s3_client.head_object(Bucket=result_bucket, Key=result_key)
        result_bytes = head.get("ContentLength", 0)
    except Exception:
        result_bytes = 0

    def _rows() -> Iterator[str]:
        for key in _iter_query_result_keys(s3_client, result_location):
            yield f"{source_bucket},{quote(key, safe='/')}\n"

    backup_dt_str = backup_dt or "none (first backup)"
    logger.info(
        "Athena inventory diff complete for %s/%s: "
        "source_dt=%s, backup_dt=%s, query_id=%s, result_size=%s bytes",
        source_alias, source_bucket, source_dt, backup_dt_str, query_id, result_bytes,
    )
    return _rows(), source_dt, backup_dt or "none", result_bytes
