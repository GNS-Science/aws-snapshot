"""Athena-backed helpers for inventory-based manifest preparation."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import boto3

from nzshm_backup.integrity import OPERATIONAL_PREFIXES
from nzshm_backup.inventory_state import _expected_prefix

logger = logging.getLogger(__name__)

_SYMLINK_DT_RE = re.compile(r"/hive/dt=([^/]+)/symlink\.txt$")

# S3 multipart-copy minimum part size (5 MB), except for the last part.
_MIN_PART_BYTES = 5 * 1024 * 1024

# SQL fragment for filtering current (non-deleted) objects in inventory.
# Non-versioned buckets have NULL is_latest/is_delete_marker.
_VERSION_FILTER = (
    "(is_latest = true OR is_latest IS NULL)"
    " AND (is_delete_marker = false OR is_delete_marker IS NULL)"
)

# All characters that urllib.parse.quote(key, safe='/') encodes.
# '%' MUST be first to avoid double-encoding.  Generated from:
#   [(c, quote(c, safe='/')) for c in map(chr, range(32,127)) if quote(c, safe='/') != c]
_URL_ENCODE_PAIRS = [
    ("%", "%25"),  # must be first
    (" ", "%20"),
    ("!", "%21"),
    ('"', "%22"),
    ("#", "%23"),
    ("$", "%24"),
    ("&", "%26"),
    ("'", "%27"),
    ("(", "%28"),
    (")", "%29"),
    ("*", "%2A"),
    ("+", "%2B"),
    (",", "%2C"),
    (":", "%3A"),
    (";", "%3B"),
    ("<", "%3C"),
    ("=", "%3D"),
    (">", "%3E"),
    ("?", "%3F"),
    ("@", "%40"),
    ("[", "%5B"),
    ("\\", "%5C"),
    ("]", "%5D"),
    ("^", "%5E"),
    ("`", "%60"),
    ("{", "%7B"),
    ("|", "%7C"),
    ("}", "%7D"),
]


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
        raise ValueError(
            f"No inventory data under s3://{control_bucket}/{prefix} — "
            f"run 'backup setup inventory' or wait for first daily delivery (~24h)"
        )

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
    return str(response["QueryExecutionId"])


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

    # Drop and recreate table to pick up schema changes (e.g. new columns).
    # Partitions are re-added per run anyway.
    drop = f"DROP TABLE IF EXISTS {table_name}"
    qid = _run_athena_query(athena_client, drop, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)

    location = f"s3://{control_bucket}/{hive_root}"
    create_table = f"""
CREATE EXTERNAL TABLE {table_name} (
  bucket string,
  key string,
  version_id string,
  is_latest boolean,
  is_delete_marker boolean,
  size bigint,
  last_modified_date timestamp,
  e_tag string,
  storage_class string,
  checksum_algorithm string
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
        f"ALTER TABLE {table_name} ADD IF NOT EXISTS PARTITION (dt='{dt}') LOCATION '{location}'"
    )
    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)


# ---------------------------------------------------------------------------
# URL-encoding helper for Athena SQL
# ---------------------------------------------------------------------------


def _build_url_encode_sql(column: str) -> str:
    """Build a nested REPLACE() chain that URL-encodes special characters.

    Equivalent to Python's ``urllib.parse.quote(value, safe='/')``.
    ``%`` is encoded first to prevent double-encoding.
    Single quotes are escaped as '' in SQL string literals.
    """
    expr = column
    for char, encoded in _URL_ENCODE_PAIRS:
        # Escape single quote for SQL string literal
        sql_char = "''" if char == "'" else char
        expr = f"REPLACE({expr}, '{sql_char}', '{encoded}')"
    return expr


def url_encode_via_replace(value: str) -> str:
    """Python-side equivalent of the SQL REPLACE chain (for testing)."""
    for char, encoded in _URL_ENCODE_PAIRS:
        value = value.replace(char, encoded)
    return value


# ---------------------------------------------------------------------------
# UNLOAD query builders
# ---------------------------------------------------------------------------

# Diff condition for incremental queries.  Used by both UNLOAD and COUNT.
# Smart ETag comparison: only compare ETags when both are single-part
# (no '-N' suffix), since multipart ETags depend on upload chunk size and
# are not content-deterministic.  Falls back to size-only when either side
# is multipart.  When checksum_algorithm is available on both sides, a
# future extension can compare content checksums instead.
# Multipart ETags contain a hyphen (e.g. "abc123-2"), single-part do not.
# strpos() returns 0 when not found in Athena/Presto.
_DIFF_WHERE = """
WHERE d.key IS NULL
   OR s.size <> d.size
   OR (
       strpos(s.e_tag, '-') = 0
       AND strpos(d.e_tag, '-') = 0
       AND s.e_tag <> d.e_tag
   )
""".rstrip()


def _build_unload_query(
    source_bucket: str,
    source_table: str,
    src_filter: str,
    unload_location: str,
    backup_table: str | None = None,
    dst_filter: str | None = None,
) -> str:
    """Build an Athena UNLOAD statement that writes manifest CSV to S3."""
    encoded_key = _build_url_encode_sql("key" if not dst_filter else "s.key")

    if backup_table and dst_filter:
        select = f"""
WITH src AS (
  SELECT key, size, e_tag
  FROM {source_table}
  WHERE {src_filter}
),
dst AS (
  SELECT key, size, e_tag
  FROM {backup_table}
  WHERE {dst_filter}
)
SELECT '{source_bucket}' AS bucket,
       {encoded_key} AS key
FROM src s
LEFT JOIN dst d ON s.key = d.key
{_DIFF_WHERE}
""".strip()
    else:
        select = f"""
SELECT '{source_bucket}' AS bucket,
       {_build_url_encode_sql('key')} AS key
FROM {source_table}
WHERE {src_filter}
""".strip()

    return f"""
UNLOAD ({select})
TO '{unload_location}'
WITH (format = 'TEXTFILE', field_delimiter = ',', compression = 'NONE')
""".strip()


def _build_count_query(
    source_table: str,
    src_filter: str,
    backup_table: str | None = None,
    dst_filter: str | None = None,
) -> str:
    """Build a SELECT COUNT(*) query matching the UNLOAD diff logic."""
    if backup_table and dst_filter:
        return f"""
WITH src AS (
  SELECT key, size, e_tag
  FROM {source_table}
  WHERE {src_filter}
),
dst AS (
  SELECT key, size, e_tag
  FROM {backup_table}
  WHERE {dst_filter}
)
SELECT COUNT(*) AS cnt
FROM src s
LEFT JOIN dst d ON s.key = d.key
{_DIFF_WHERE}
""".strip()
    else:
        return f"""
SELECT COUNT(*) AS cnt
FROM {source_table}
WHERE {src_filter}
""".strip()


# ---------------------------------------------------------------------------
# S3 multipart-copy concatenation
# ---------------------------------------------------------------------------


def _list_unload_parts(s3_client, bucket: str, prefix: str) -> list[dict]:
    """List all UNLOAD output objects under *prefix*, sorted by key."""
    parts: list[dict] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            if obj.get("Size", 0) > 0:
                parts.append(obj)
    parts.sort(key=lambda o: o["Key"])
    return parts


def _concat_unload_parts(
    s3_client,
    control_bucket: str,
    unload_prefix: str,
    dest_bucket: str,
    manifest_key: str,
) -> tuple[str, int] | None:
    """Concatenate UNLOAD output files into a single manifest.

    Returns ``(etag, total_bytes)`` or ``None`` if no parts exist (empty result).
    Uses S3 multipart-copy when possible (no data through Lambda memory).
    Falls back to download+upload for parts smaller than 5 MB.
    """
    parts = _list_unload_parts(s3_client, control_bucket, unload_prefix)
    if not parts:
        return None

    # Single file — simple copy
    if len(parts) == 1:
        resp = s3_client.copy_object(
            CopySource={"Bucket": control_bucket, "Key": parts[0]["Key"]},
            Bucket=dest_bucket,
            Key=manifest_key,
            ContentType="text/csv",
        )
        return resp["CopyObjectResult"]["ETag"], parts[0]["Size"]

    # Check if all non-last parts meet the 5 MB minimum for UploadPartCopy
    can_multipart_copy = all(p["Size"] >= _MIN_PART_BYTES for p in parts[:-1])

    if can_multipart_copy:
        return _multipart_copy_concat(s3_client, control_bucket, parts, dest_bucket, manifest_key)
    else:
        return _download_and_upload(s3_client, control_bucket, parts, dest_bucket, manifest_key)


def _multipart_copy_concat(
    s3_client,
    source_bucket: str,
    parts: list[dict],
    dest_bucket: str,
    manifest_key: str,
) -> tuple[str, int]:
    """Concatenate via S3 UploadPartCopy (server-side, no data through Lambda)."""
    mpu = s3_client.create_multipart_upload(
        Bucket=dest_bucket,
        Key=manifest_key,
        ContentType="text/csv",
    )
    upload_id = mpu["UploadId"]
    uploaded: list[dict] = []
    total_bytes = 0

    try:
        for i, part in enumerate(parts, 1):
            resp = s3_client.upload_part_copy(
                Bucket=dest_bucket,
                Key=manifest_key,
                UploadId=upload_id,
                PartNumber=i,
                CopySource={"Bucket": source_bucket, "Key": part["Key"]},
            )
            uploaded.append({"PartNumber": i, "ETag": resp["CopyPartResult"]["ETag"]})
            total_bytes += part["Size"]

        result = s3_client.complete_multipart_upload(
            Bucket=dest_bucket,
            Key=manifest_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": uploaded},
        )
        return result["ETag"], total_bytes
    except Exception:
        s3_client.abort_multipart_upload(
            Bucket=dest_bucket,
            Key=manifest_key,
            UploadId=upload_id,
        )
        raise


def _download_and_upload(
    s3_client,
    source_bucket: str,
    parts: list[dict],
    dest_bucket: str,
    manifest_key: str,
) -> tuple[str, int]:
    """Fallback: download small parts and upload as a single object."""
    buf = b""
    for part in parts:
        body = s3_client.get_object(Bucket=source_bucket, Key=part["Key"])["Body"]
        buf += body.read()

    resp = s3_client.put_object(
        Bucket=dest_bucket,
        Key=manifest_key,
        Body=buf,
        ContentType="text/csv",
    )
    return resp["ETag"], len(buf)


def _cleanup_unload_parts(s3_client, bucket: str, prefix: str) -> None:
    """Delete ALL objects under the UNLOAD prefix (including 0-byte markers).

    Raises RuntimeError if S3 returns any per-key error. ``Quiet: True``
    suppresses successful-delete entries in the response but still returns
    the ``Errors`` list — silently ignoring it once let an ``AccessDenied``
    leave a stale part file that blocked subsequent UNLOAD runs with
    HIVE_PATH_ALREADY_EXISTS (see issue #40).
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            keys.append({"Key": obj["Key"]})
    if not keys:
        return
    # delete_objects accepts max 1000 keys per call
    for i in range(0, len(keys), 1000):
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": keys[i : i + 1000], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            first = errors[0]
            raise RuntimeError(
                f"Failed to delete {len(errors)} UNLOAD part(s) under "
                f"s3://{bucket}/{prefix}: first error key={first.get('Key')!r} "
                f"code={first.get('Code')!r} message={first.get('Message')!r}"
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_inventory_manifest_via_athena(
    session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
    manifest_bucket: str,
    manifest_key: str,
    full_sync: bool = False,
) -> tuple[str | None, str, str | None, int]:
    """Build an S3 Batch manifest via Athena UNLOAD.

    Returns:
        (manifest_etag, source_dt, backup_dt, row_count)
        manifest_etag is None when row_count == 0 (nothing to copy).
    """
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

    # Ensure tables and partitions
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
    if backup_dt is not None and backup_hive_root is not None:
        _ensure_partition(
            athena_client,
            output_location,
            database,
            backup_table,
            control_bucket,
            backup_hive_root,
            backup_dt,
        )

    # Build filters
    _src_filter = f"dt = '{source_dt}' AND {_VERSION_FILTER}"
    _prefix_exclusions = " AND ".join(f"key NOT LIKE '{p}%'" for p in OPERATIONAL_PREFIXES)
    _dst_filter = (
        (f"dt = '{backup_dt}' AND {_VERSION_FILTER} AND {_prefix_exclusions}")
        if backup_dt
        else None
    )

    _backup_table = backup_table if backup_dt else None

    # Run UNLOAD and COUNT(*) in parallel
    unload_prefix = f"_manifests/unload/{source_alias}/{source_bucket}/"
    # Clean any stale output from a previous failed run
    _cleanup_unload_parts(s3_client, control_bucket, unload_prefix)

    unload_location = f"s3://{control_bucket}/{unload_prefix}"
    unload_query = _build_unload_query(
        source_bucket,
        source_table,
        _src_filter,
        unload_location,
        backup_table=_backup_table,
        dst_filter=_dst_filter,
    )
    count_query = _build_count_query(
        source_table,
        _src_filter,
        backup_table=_backup_table,
        dst_filter=_dst_filter,
    )

    unload_qid = _run_athena_query(
        athena_client,
        unload_query,
        output_location,
        database=database,
    )
    count_qid = _run_athena_query(
        athena_client,
        count_query,
        output_location,
        database=database,
    )
    logger.info(
        "Athena UNLOAD started for %s/%s: unload_qid=%s, count_qid=%s",
        source_alias,
        source_bucket,
        unload_qid,
        count_qid,
    )

    _wait_for_athena_query(athena_client, unload_qid)
    _wait_for_athena_query(athena_client, count_qid)

    # Read row count from COUNT(*) result
    count_exec = athena_client.get_query_execution(QueryExecutionId=count_qid)
    count_loc = count_exec["QueryExecution"]["ResultConfiguration"]["OutputLocation"]
    row_count = _read_count_result(s3_client, count_loc)

    backup_dt_str = backup_dt or "none (first backup)"
    logger.info(
        "Athena UNLOAD complete for %s/%s: source_dt=%s, backup_dt=%s, row_count=%s, unload_qid=%s",
        source_alias,
        source_bucket,
        source_dt,
        backup_dt_str,
        f"{row_count:,}",
        unload_qid,
    )

    if row_count == 0:
        _cleanup_unload_parts(s3_client, control_bucket, unload_prefix)
        return None, source_dt, backup_dt, 0

    # Concatenate UNLOAD parts into single manifest
    result = _concat_unload_parts(
        s3_client,
        control_bucket,
        unload_prefix,
        manifest_bucket,
        manifest_key,
    )
    _cleanup_unload_parts(s3_client, control_bucket, unload_prefix)

    if result is None:
        logger.warning("UNLOAD reported %d rows but no output files found", row_count)
        return None, source_dt, backup_dt, 0

    manifest_etag, total_bytes = result
    logger.info(
        "Manifest concatenated: s3://%s/%s (%s bytes, %s rows, etag=%s)",
        manifest_bucket,
        manifest_key,
        f"{total_bytes:,}",
        f"{row_count:,}",
        manifest_etag,
    )
    return manifest_etag, source_dt, backup_dt, row_count


def _read_count_result(s3_client, result_location: str) -> int:
    """Read the scalar result from a COUNT(*) Athena query."""
    bucket, key = _parse_s3_uri(result_location)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # First line is header ("cnt"), second is the value.
    # Athena may quote the value, e.g. "39973875".
    if len(lines) >= 2:
        try:
            return int(lines[1].strip('"'))
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# Inventory-based random sampling for test commands
# ---------------------------------------------------------------------------

_ARCHIVED_STORAGE_CLASSES = frozenset(
    {
        "GLACIER",
        "DEEP_ARCHIVE",
        "GLACIER_IR",
    }
)


def sample_objects_via_inventory(
    session: boto3.Session,
    source_alias: str,
    backup_bucket: str,
    sample_size: int = 10,
) -> list[dict]:
    """Return a random sample of backup objects using Athena inventory queries.

    Returns a list of dicts with ``Key``, ``ETag``, and ``Size`` fields,
    matching the shape expected by the test restore command.

    Raises ``ValueError`` if no inventory partitions are available.
    """
    import csv as csv_mod

    s3_client = session.client("s3")
    athena_client = session.client("athena")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"

    backup_prefix = _expected_prefix(source_alias, "backup", backup_bucket)
    backup_dt, backup_hive_root = _latest_inventory_partition(
        s3_client,
        control_bucket,
        backup_prefix,
    )

    database = "nzshm_backup_inventory"
    table = _table_name(source_alias, "backup", backup_bucket)
    output_location = f"s3://{control_bucket}/athena-results/{source_alias}/{backup_bucket}/"

    _ensure_inventory_table(
        athena_client,
        output_location,
        database,
        table,
        control_bucket,
        backup_hive_root,
    )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        table,
        control_bucket,
        backup_hive_root,
        backup_dt,
    )

    # Build exclusion filters
    prefix_filters = " AND ".join(f"key NOT LIKE '{p}%'" for p in OPERATIONAL_PREFIXES)
    storage_filters = " AND ".join(
        f"storage_class <> '{sc}'" for sc in sorted(_ARCHIVED_STORAGE_CLASSES)
    )

    query = f"""
SELECT key, e_tag, size
FROM {table}
WHERE dt = '{backup_dt}'
  AND {_VERSION_FILTER}
  AND {prefix_filters}
  AND ({storage_filters} OR storage_class IS NULL)
ORDER BY RAND()
LIMIT {sample_size}
""".strip()

    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)

    execution = athena_client.get_query_execution(QueryExecutionId=qid)
    result_location = execution["QueryExecution"]["ResultConfiguration"]["OutputLocation"]

    # Read Athena result CSV
    result_bucket, result_key = _parse_s3_uri(result_location)
    body = s3_client.get_object(Bucket=result_bucket, Key=result_key)["Body"]
    text = body.read().decode("utf-8")
    reader = csv_mod.reader(text.splitlines())

    objects: list[dict] = []
    first = True
    for row in reader:
        if first:
            first = False
            continue
        if len(row) >= 3 and row[0]:
            etag = row[1].strip('"')
            if not etag.startswith('"'):
                etag = f'"{etag}"'
            objects.append(
                {
                    "Key": row[0],
                    "ETag": etag,
                    "Size": int(row[2]) if row[2] else 0,
                }
            )

    logger.info(
        "Sampled %d objects from inventory (dt=%s) for %s/%s",
        len(objects),
        backup_dt,
        source_alias,
        backup_bucket,
    )
    return objects


# ---------------------------------------------------------------------------
# Object-count delta — daily health report (ADR-006 mitigation 1)
# ---------------------------------------------------------------------------


def _two_latest_inventory_partitions(
    s3_client,
    control_bucket: str,
    inventory_prefix: str,
) -> list[tuple[str, str]]:
    """Return up to two most recent (dt, hive_root) tuples, newest first.

    Same scan as _latest_inventory_partition but keeps the two newest dt
    values for delta calculations.
    """
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

    sorted_dts = sorted(dt_to_symlink, reverse=True)[:2]
    out: list[tuple[str, str]] = []
    for dt in sorted_dts:
        symlink_key = dt_to_symlink[dt]
        hive_root = symlink_key.split("/hive/dt=")[0] + "/hive/"
        out.append((dt, hive_root))
    return out


def count_objects_for_partition(
    session: boto3.Session,
    source_alias: str,
    side: str,
    bucket: str,
    dt: str,
    hive_root: str,
) -> int:
    """COUNT(*) (non-current-version filtered) for one inventory dt partition.

    Reuses the same Glue database, table name, workgroup output location,
    and version filter as the manifest pipeline — so Athena scan-bytes
    accounting and cost limits remain consistent.

    Args:
        side: "source" or "backup".
    """
    s3_client = session.client("s3")
    athena_client = session.client("athena")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"
    database = "nzshm_backup_inventory"
    table = _table_name(source_alias, side, bucket)
    output_location = f"s3://{control_bucket}/athena-results/{source_alias}/{bucket}/"

    _ensure_inventory_table(
        athena_client, output_location, database, table, control_bucket, hive_root
    )
    _ensure_partition(
        athena_client, output_location, database, table, control_bucket, hive_root, dt
    )

    query = _build_count_query(table, f"dt = '{dt}' AND {_VERSION_FILTER}")
    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)
    exec_info = athena_client.get_query_execution(QueryExecutionId=qid)
    result_loc = exec_info["QueryExecution"]["ResultConfiguration"]["OutputLocation"]
    return _read_count_result(s3_client, result_loc)


def count_delta(
    session: boto3.Session,
    source_alias: str,
    side: str,
    bucket: str,
) -> dict[str, Any]:
    """Return today vs yesterday object counts and the delta.

    Used by the daily health report to flag large drops (ADR-006
    mitigation 1: catches intentional source deletions that under
    ADR-006's no-Expiration proposal would otherwise persist in backup
    forever).

    Returns::

        {
            "today_dt": str | None,
            "today_count": int | None,
            "yesterday_dt": str | None,
            "yesterday_count": int | None,
            "delta": int | None,           # today - yesterday
            "delta_pct": float | None,     # (today - yesterday) / yesterday * 100
            "available": bool,             # True iff both partitions queried
        }

    When fewer than two partitions exist (e.g. first day after Inventory
    enabled), the missing fields are None and ``available`` is False.
    """
    s3_client = session.client("s3")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"
    inventory_prefix = _expected_prefix(source_alias, side, bucket)

    partitions = _two_latest_inventory_partitions(s3_client, control_bucket, inventory_prefix)
    if not partitions:
        return {
            "today_dt": None,
            "today_count": None,
            "yesterday_dt": None,
            "yesterday_count": None,
            "delta": None,
            "delta_pct": None,
            "available": False,
        }

    today_dt, today_hive = partitions[0]
    today_count = count_objects_for_partition(
        session, source_alias, side, bucket, today_dt, today_hive
    )

    if len(partitions) < 2:
        return {
            "today_dt": today_dt,
            "today_count": today_count,
            "yesterday_dt": None,
            "yesterday_count": None,
            "delta": None,
            "delta_pct": None,
            "available": False,
        }

    yesterday_dt, yesterday_hive = partitions[1]
    yesterday_count = count_objects_for_partition(
        session, source_alias, side, bucket, yesterday_dt, yesterday_hive
    )

    delta = today_count - yesterday_count
    delta_pct = (delta / yesterday_count * 100.0) if yesterday_count else None

    return {
        "today_dt": today_dt,
        "today_count": today_count,
        "yesterday_dt": yesterday_dt,
        "yesterday_count": yesterday_count,
        "delta": delta,
        "delta_pct": delta_pct,
        "available": True,
    }


def _build_divergence_count_query(
    source_table: str,
    source_filter: str,
    backup_table: str,
    backup_filter: str,
) -> str:
    """One-scan query returning ``source - backup`` and ``backup - source`` key counts.

    A FULL OUTER JOIN on key lets a single Athena scan satisfy both
    directions of the divergence — class-1 (backup-missing) and class-2
    (backup-orphans) for ADR-009. Reusing the manifest pipeline's prefix
    exclusions on the backup side keeps operational keys (`_state/`,
    `_manifests/`, `_events/`, `_inventory/`) out of both counts.
    """
    return f"""
WITH src AS (
  SELECT key
  FROM {source_table}
  WHERE {source_filter}
),
bkp AS (
  SELECT key
  FROM {backup_table}
  WHERE {backup_filter}
)
SELECT
  COUNT_IF(b.key IS NULL) AS source_minus_backup,
  COUNT_IF(s.key IS NULL) AS backup_minus_source
FROM src s
FULL OUTER JOIN bkp b ON s.key = b.key
""".strip()


def _read_two_count_result(s3_client, result_location: str) -> tuple[int, int]:
    """Read the (source_minus_backup, backup_minus_source) row from a 2-col Athena result."""
    bucket, key = _parse_s3_uri(result_location)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"Unexpected Athena divergence result at {result_location}: {body!r}")
    # Skip header line; data row is "smb,bms" (quoted ints).
    data = lines[1].replace('"', "").split(",")
    return int(data[0]), int(data[1])


def divergence_counts(
    session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
) -> dict[str, Any]:
    """Source-vs-backup key divergence in both directions (ADR-009).

    Compares the latest source-side inventory partition against the
    latest backup-side inventory partition for the configured
    ``source_alias`` and returns::

        {
            "source_dt": str | None,
            "backup_dt": str | None,
            "source_minus_backup": int | None,   # class-1 (red): backup is missing these keys
            "backup_minus_source": int | None,   # class-2 (info): backup-side orphans
            "available": bool,                   # True iff both partitions queried
        }

    When either side has no inventory partition yet, ``available`` is
    ``False`` and the count fields are ``None``.

    Reuses the same Glue database, table naming, workgroup output
    location, version filter and operational-prefix exclusion list as
    the manifest pipeline (``build_inventory_manifest_via_athena``), so
    scan-bytes accounting and cost caps stay consistent.
    """
    s3_client = session.client("s3")
    athena_client = session.client("athena")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"
    database = "nzshm_backup_inventory"
    output_location = f"s3://{control_bucket}/athena-results/{source_alias}/{source_bucket}/"

    source_prefix = _expected_prefix(source_alias, "source", source_bucket)
    backup_prefix = _expected_prefix(source_alias, "backup", backup_bucket)

    try:
        source_dt, source_hive = _latest_inventory_partition(
            s3_client, control_bucket, source_prefix
        )
    except ValueError:
        source_dt, source_hive = None, None
    try:
        backup_dt, backup_hive = _latest_inventory_partition(
            s3_client, control_bucket, backup_prefix
        )
    except ValueError:
        backup_dt, backup_hive = None, None

    if source_dt is None or backup_dt is None:
        return {
            "source_dt": source_dt,
            "backup_dt": backup_dt,
            "source_minus_backup": None,
            "backup_minus_source": None,
            "available": False,
        }
    assert source_hive is not None and backup_hive is not None  # for type narrowing

    source_table = _table_name(source_alias, "source", source_bucket)
    backup_table = _table_name(source_alias, "backup", backup_bucket)

    _ensure_inventory_table(
        athena_client, output_location, database, source_table, control_bucket, source_hive
    )
    _ensure_inventory_table(
        athena_client, output_location, database, backup_table, control_bucket, backup_hive
    )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        source_table,
        control_bucket,
        source_hive,
        source_dt,
    )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        backup_table,
        control_bucket,
        backup_hive,
        backup_dt,
    )

    prefix_exclusions = " AND ".join(f"key NOT LIKE '{p}%'" for p in OPERATIONAL_PREFIXES)
    source_filter = f"dt = '{source_dt}' AND {_VERSION_FILTER}"
    backup_filter = f"dt = '{backup_dt}' AND {_VERSION_FILTER} AND {prefix_exclusions}"

    query = _build_divergence_count_query(source_table, source_filter, backup_table, backup_filter)
    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)
    exec_info = athena_client.get_query_execution(QueryExecutionId=qid)
    result_loc = exec_info["QueryExecution"]["ResultConfiguration"]["OutputLocation"]
    smb, bms = _read_two_count_result(s3_client, result_loc)

    return {
        "source_dt": source_dt,
        "backup_dt": backup_dt,
        "source_minus_backup": smb,
        "backup_minus_source": bms,
        "available": True,
    }


def _build_divergence_sample_query(
    source_table: str,
    source_filter: str,
    backup_table: str,
    backup_filter: str,
    limit: int = 10,
) -> str:
    """Sample up to ``limit`` keys present in source but missing from backup.

    Companion to ``_build_divergence_count_query``. Single-direction
    (class-1 red): keys ``divergence_counts`` is counting in
    ``source_minus_backup``. The health-report orchestrator head_objects
    each returned key against the live backup bucket to distinguish
    "still missing" from "auto-healed since snapshot".
    """
    return f"""
WITH src AS (
  SELECT key
  FROM {source_table}
  WHERE {source_filter}
),
bkp AS (
  SELECT key
  FROM {backup_table}
  WHERE {backup_filter}
)
SELECT s.key
FROM src s
LEFT JOIN bkp b ON s.key = b.key
WHERE b.key IS NULL
LIMIT {limit}
""".strip()


def _read_key_list_result(s3_client, result_location: str) -> list[str]:
    """Read a single-column CSV result (header 'key', then N data rows)."""
    import csv as csv_mod

    bucket, key = _parse_s3_uri(result_location)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    reader = csv_mod.reader(body.splitlines())
    keys: list[str] = []
    first = True
    for row in reader:
        if first:
            first = False
            continue
        if row and row[0]:
            keys.append(row[0])
    return keys


def divergence_sample_keys(
    session: boto3.Session,
    source_alias: str,
    source_bucket: str,
    backup_bucket: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Sample up to ``limit`` keys present in source but missing from backup.

    Companion to ``divergence_counts`` for ADR-009 class-1 RED tagging.
    When ``divergence_counts`` reports ``source_minus_backup > 0``, this
    function returns up to ``limit`` actual key names so the daily
    health-report orchestrator can ``head_object`` each against the live
    backup bucket and tag the class-1 RED signal with "(still missing
    live)" or "(auto-healed since snapshot)".

    Reuses the same Glue database, table naming, workgroup output
    location, version filter, and operational-prefix exclusion list as
    ``divergence_counts``, so cost accounting and behaviour stay
    consistent.

    Returns::

        {
            "source_dt": str | None,
            "backup_dt": str | None,
            "source_minus_backup_sample": list[str],
            "sample_size": int,    # actual returned count (may be < limit)
            "available": bool,     # True iff both partitions queried
        }
    """
    s3_client = session.client("s3")
    athena_client = session.client("athena")
    account_id = session.client("sts").get_caller_identity()["Account"]
    control_bucket = f"nzshm-backup-inventory-{account_id}"
    database = "nzshm_backup_inventory"
    output_location = f"s3://{control_bucket}/athena-results/{source_alias}/{source_bucket}/"

    source_prefix = _expected_prefix(source_alias, "source", source_bucket)
    backup_prefix = _expected_prefix(source_alias, "backup", backup_bucket)

    try:
        source_dt, source_hive = _latest_inventory_partition(
            s3_client, control_bucket, source_prefix
        )
    except ValueError:
        source_dt, source_hive = None, None
    try:
        backup_dt, backup_hive = _latest_inventory_partition(
            s3_client, control_bucket, backup_prefix
        )
    except ValueError:
        backup_dt, backup_hive = None, None

    if source_dt is None or backup_dt is None:
        return {
            "source_dt": source_dt,
            "backup_dt": backup_dt,
            "source_minus_backup_sample": [],
            "sample_size": 0,
            "available": False,
        }
    assert source_hive is not None and backup_hive is not None  # for type narrowing

    source_table = _table_name(source_alias, "source", source_bucket)
    backup_table = _table_name(source_alias, "backup", backup_bucket)

    _ensure_inventory_table(
        athena_client, output_location, database, source_table, control_bucket, source_hive
    )
    _ensure_inventory_table(
        athena_client, output_location, database, backup_table, control_bucket, backup_hive
    )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        source_table,
        control_bucket,
        source_hive,
        source_dt,
    )
    _ensure_partition(
        athena_client,
        output_location,
        database,
        backup_table,
        control_bucket,
        backup_hive,
        backup_dt,
    )

    prefix_exclusions = " AND ".join(f"key NOT LIKE '{p}%'" for p in OPERATIONAL_PREFIXES)
    source_filter = f"dt = '{source_dt}' AND {_VERSION_FILTER}"
    backup_filter = f"dt = '{backup_dt}' AND {_VERSION_FILTER} AND {prefix_exclusions}"

    query = _build_divergence_sample_query(
        source_table, source_filter, backup_table, backup_filter, limit
    )
    qid = _run_athena_query(athena_client, query, output_location, database=database)
    _wait_for_athena_query(athena_client, qid)
    exec_info = athena_client.get_query_execution(QueryExecutionId=qid)
    result_loc = exec_info["QueryExecution"]["ResultConfiguration"]["OutputLocation"]
    keys = _read_key_list_result(s3_client, result_loc)

    return {
        "source_dt": source_dt,
        "backup_dt": backup_dt,
        "source_minus_backup_sample": keys,
        "sample_size": len(keys),
        "available": True,
    }
