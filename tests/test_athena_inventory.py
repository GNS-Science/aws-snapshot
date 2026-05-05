"""Tests for Athena-backed inventory manifest helpers."""

from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

from nzshm_backup import athena_inventory as ai


def test_latest_inventory_partition_detects_latest_dt_and_hive_root():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                {
                    "Key": (
                        "inventory/ths/source/ths-dataset-prod/ths-dataset-prod/"
                        "nzshm-ths-src-dataset-prod/hive/dt=2026-04-21-01-00/symlink.txt"
                    )
                },
                {
                    "Key": (
                        "inventory/ths/source/ths-dataset-prod/ths-dataset-prod/"
                        "nzshm-ths-src-dataset-prod/hive/dt=2026-04-22-01-00/symlink.txt"
                    )
                },
            ]
        }
    ]

    dt, hive_root = ai._latest_inventory_partition(
        s3,
        "nzshm-backup-inventory-123",
        "inventory/ths/source/ths-dataset-prod",
    )

    assert dt == "2026-04-22-01-00"
    assert hive_root.endswith("nzshm-ths-src-dataset-prod/hive/")


# ---------------------------------------------------------------------------
# URL-encoding helpers
# ---------------------------------------------------------------------------


def test_build_url_encode_sql_contains_all_replacements():
    sql = ai._build_url_encode_sql("key")
    for _char, encoded in ai._URL_ENCODE_PAIRS:
        assert encoded in sql


def test_build_url_encode_sql_encodes_percent_first():
    """% must be the innermost REPLACE to avoid double-encoding."""
    sql = ai._build_url_encode_sql("key")
    # The innermost REPLACE is the first applied — it should be %→%25
    assert "REPLACE(key, '%', '%25')" in sql


@pytest.mark.parametrize(
    "key",
    [
        "simple/path.txt",
        "folder/file 1.txt",
        "vs30=1000/imt=SA(0.15)/result.csv",
        'key with "quotes"',
        "path/with#hash",
        "already%encoded.txt",
        "key,with,commas.txt",
        "all special % , = ( ) \" # chars.txt",
        "Mw=logA+C+equations.pdf",
        "key+with+plus.txt",
    ],
)
def test_url_encode_via_replace_matches_python_quote(key):
    """The SQL REPLACE chain must match urllib.parse.quote(key, safe='/')."""
    expected = quote(key, safe="/")
    actual = ai.url_encode_via_replace(key)
    assert actual == expected, f"Mismatch for {key!r}: {actual!r} != {expected!r}"


# ---------------------------------------------------------------------------
# UNLOAD query builders
# ---------------------------------------------------------------------------


def test_build_unload_query_full_sync():
    q = ai._build_unload_query(
        "my-bucket", "inv_src", "dt = '2026-05-01'",
        "s3://ctrl/_manifests/unload/test/",
    )
    assert "UNLOAD" in q
    assert "'my-bucket' AS bucket" in q
    assert "inv_src" in q
    assert "s3://ctrl/_manifests/unload/test/" in q
    assert "LEFT JOIN" not in q


def test_build_unload_query_incremental_diff():
    q = ai._build_unload_query(
        "my-bucket", "inv_src", "dt = '2026-05-01'",
        "s3://ctrl/_manifests/unload/test/",
        backup_table="inv_dst", dst_filter="dt = '2026-05-01'",
    )
    assert "UNLOAD" in q
    assert "LEFT JOIN" in q
    assert "inv_dst" in q
    assert "'my-bucket' AS bucket" in q
    # Smart ETag: only compare when both are single-part (no hyphen = not multipart)
    assert "strpos" in q
    assert "s.e_tag <> d.e_tag" in q


def test_build_count_query_full_sync():
    q = ai._build_count_query("inv_src", "dt = '2026-05-01'")
    assert "COUNT(*)" in q
    assert "LEFT JOIN" not in q


def test_build_count_query_incremental_diff():
    q = ai._build_count_query(
        "inv_src", "dt = '2026-05-01'",
        backup_table="inv_dst", dst_filter="dt = '2026-05-01'",
    )
    assert "COUNT(*)" in q
    assert "LEFT JOIN" in q
    assert "strpos" in q  # smart ETag comparison


# ---------------------------------------------------------------------------
# S3 multipart-copy concatenation
# ---------------------------------------------------------------------------


def test_concat_unload_parts_empty():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
    result = ai._concat_unload_parts(s3, "ctrl", "prefix/", "dest", "manifest.csv")
    assert result is None


def test_concat_unload_parts_single_file():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "prefix/part-00000.txt", "Size": 1024}]}
    ]
    s3.copy_object.return_value = {"CopyObjectResult": {"ETag": '"abc123"'}}

    etag, size = ai._concat_unload_parts(s3, "ctrl", "prefix/", "dest", "manifest.csv")
    assert etag == '"abc123"'
    assert size == 1024
    s3.copy_object.assert_called_once()


def test_concat_unload_parts_multiple_large_files():
    s3 = MagicMock()
    parts = [
        {"Key": f"prefix/part-{i:05d}.txt", "Size": 10 * 1024 * 1024}
        for i in range(3)
    ]
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": parts}]
    s3.create_multipart_upload.return_value = {"UploadId": "up-123"}
    s3.upload_part_copy.return_value = {"CopyPartResult": {"ETag": '"p1"'}}
    s3.complete_multipart_upload.return_value = {"ETag": '"final"'}

    etag, total = ai._concat_unload_parts(s3, "ctrl", "prefix/", "dest", "manifest.csv")
    assert etag == '"final"'
    assert total == 30 * 1024 * 1024
    assert s3.upload_part_copy.call_count == 3


def test_concat_unload_parts_small_files_fallback():
    s3 = MagicMock()
    parts = [
        {"Key": "prefix/part-00000.txt", "Size": 100},
        {"Key": "prefix/part-00001.txt", "Size": 200},
    ]
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": parts}]

    body1 = MagicMock()
    body1.read.return_value = b"bucket,key1\n"
    body2 = MagicMock()
    body2.read.return_value = b"bucket,key2\n"
    s3.get_object.side_effect = [{"Body": body1}, {"Body": body2}]
    s3.put_object.return_value = {"ETag": '"small"'}

    etag, total = ai._concat_unload_parts(s3, "ctrl", "prefix/", "dest", "manifest.csv")
    assert etag == '"small"'
    assert total == 24  # len(b"bucket,key1\nbucket,key2\n")
    s3.put_object.assert_called_once()


# ---------------------------------------------------------------------------
# read_count_result
# ---------------------------------------------------------------------------


def test_read_count_result():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b'"cnt"\n"39973875"\n'
    s3.get_object.return_value = {"Body": body}
    count = ai._read_count_result(s3, "s3://bucket/result.csv")
    assert count == 39973875


def test_read_count_result_quoted():
    """Athena may quote numeric values."""
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b'"cnt"\n"100"\n'
    s3.get_object.return_value = {"Body": body}
    count = ai._read_count_result(s3, "s3://bucket/result.csv")
    assert count == 100


# ---------------------------------------------------------------------------
# Integration: build_inventory_manifest_via_athena
# ---------------------------------------------------------------------------


def test_build_inventory_manifest_via_athena_runs_unload_and_count():
    s3 = MagicMock()
    # count result
    count_body = MagicMock()
    count_body.read.return_value = b'"cnt"\n"2"\n'
    s3.get_object.return_value = {"Body": count_body}
    # concat: single file
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "prefix/part.txt", "Size": 100}]}
    ]
    s3.copy_object.return_value = {"CopyObjectResult": {"ETag": '"manifest-etag"'}}

    athena = MagicMock()
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "ResultConfiguration": {
                "OutputLocation": "s3://ctrl/athena-results/count.csv"
            }
        }
    }
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123"}

    session = MagicMock()
    session.client.side_effect = lambda svc, **kw: {
        "s3": s3,
        "athena": athena,
        "sts": sts,
    }[svc]

    run_query = MagicMock(side_effect=["unload-qid", "count-qid"])
    with patch.object(
        ai,
        "_latest_inventory_partition",
        side_effect=[
            ("2026-04-23-01-00", "inventory/ths/source/x/hive/"),
            ("2026-04-23-01-00", "inventory/ths/backup/y/hive/"),
        ],
    ):
        with patch.object(ai, "_ensure_inventory_table"):
            with patch.object(ai, "_ensure_partition"):
                with patch.object(ai, "_run_athena_query", run_query):
                    with patch.object(ai, "_wait_for_athena_query"):
                        etag, src_dt, bkp_dt, count = (
                            ai.build_inventory_manifest_via_athena(
                                session,
                                "ths",
                                "ths-dataset-prod",
                                "bb-ths-backup",
                                manifest_bucket="bb-ths-backup",
                                manifest_key="_manifests/test.csv",
                                full_sync=False,
                            )
                        )

    assert src_dt == "2026-04-23-01-00"
    assert bkp_dt == "2026-04-23-01-00"
    assert etag == '"manifest-etag"'
    assert count == 2
    # First call is UNLOAD, second is COUNT
    assert run_query.call_count == 2
    unload_sql = run_query.call_args_list[0].args[1]
    assert "UNLOAD" in unload_sql
    count_sql = run_query.call_args_list[1].args[1]
    assert "COUNT(*)" in count_sql


# ---------------------------------------------------------------------------
# _sanitize_identifier edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("simple", "simple"),
        ("hello-world", "hello_world"),
        ("a..b..c", "a_b_c"),
        ("---", "x"),  # all non-alphanum → collapsed → stripped → fallback "x"
        ("", "x"),
        ("CamelCase", "camelcase"),
        ("inv_ths_source_bucket", "inv_ths_source_bucket"),
        ("with spaces and (parens)", "with_spaces_and_parens"),
        ("__leading__trailing__", "leading_trailing"),
    ],
)
def test_sanitize_identifier(value, expected):
    assert ai._sanitize_identifier(value) == expected


# ---------------------------------------------------------------------------
# _parse_s3_uri edge cases
# ---------------------------------------------------------------------------


def test_parse_s3_uri_valid():
    bucket, key = ai._parse_s3_uri("s3://my-bucket/path/to/file.txt")
    assert bucket == "my-bucket"
    assert key == "path/to/file.txt"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/file",
        "my-bucket/key",
        "",
        "s3://",
        "s3://bucket-only",
        "s3:///no-bucket",
    ],
)
def test_parse_s3_uri_invalid(uri):
    with pytest.raises(ValueError, match="Invalid S3 URI"):
        ai._parse_s3_uri(uri)


# ---------------------------------------------------------------------------
# _ensure_inventory_table
# ---------------------------------------------------------------------------


def test_ensure_inventory_table_runs_create_db_drop_and_create():
    """Should run CREATE DATABASE, DROP TABLE, then CREATE EXTERNAL TABLE."""
    athena = MagicMock()
    query_ids = iter(["q1", "q2", "q3"])
    athena.start_query_execution.side_effect = lambda **kw: {
        "QueryExecutionId": next(query_ids)
    }

    with patch.object(ai, "_wait_for_athena_query"):
        ai._ensure_inventory_table(
            athena,
            "s3://ctrl/output/",
            "mydb",
            "my_table",
            "ctrl-bucket",
            "inventory/hive/",
        )

    assert athena.start_query_execution.call_count == 3
    calls = athena.start_query_execution.call_args_list

    # First call: CREATE DATABASE
    assert "CREATE DATABASE" in calls[0].kwargs["QueryString"]
    # Second call: DROP TABLE
    assert "DROP TABLE" in calls[1].kwargs["QueryString"]
    assert "my_table" in calls[1].kwargs["QueryString"]
    # Third call: CREATE EXTERNAL TABLE
    assert "CREATE EXTERNAL TABLE" in calls[2].kwargs["QueryString"]
    assert "my_table" in calls[2].kwargs["QueryString"]
    assert "s3://ctrl-bucket/inventory/hive/" in calls[2].kwargs["QueryString"]


# ---------------------------------------------------------------------------
# _cleanup_unload_parts
# ---------------------------------------------------------------------------


def test_cleanup_unload_parts_no_objects():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
    ai._cleanup_unload_parts(s3, "bucket", "prefix/")
    s3.delete_objects.assert_not_called()


def test_cleanup_unload_parts_single_batch():
    s3 = MagicMock()
    keys = [{"Key": f"prefix/part-{i:05d}.txt"} for i in range(5)]
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": k["Key"]} for k in keys]}
    ]
    ai._cleanup_unload_parts(s3, "bucket", "prefix/")
    s3.delete_objects.assert_called_once()
    deleted = s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
    assert len(deleted) == 5


def test_cleanup_unload_parts_batches_over_1000():
    s3 = MagicMock()
    keys = [{"Key": f"prefix/part-{i:05d}.txt"} for i in range(1500)]
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": keys}]
    ai._cleanup_unload_parts(s3, "bucket", "prefix/")
    assert s3.delete_objects.call_count == 2
    first_batch = s3.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]
    second_batch = s3.delete_objects.call_args_list[1].kwargs["Delete"]["Objects"]
    assert len(first_batch) == 1000
    assert len(second_batch) == 500


# ---------------------------------------------------------------------------
# _multipart_copy_concat — abort on error
# ---------------------------------------------------------------------------


def test_multipart_copy_concat_aborts_on_error():
    s3 = MagicMock()
    s3.create_multipart_upload.return_value = {"UploadId": "up-err"}
    s3.upload_part_copy.side_effect = Exception("CopyFailed")

    parts = [
        {"Key": "p/a.txt", "Size": 10 * 1024 * 1024},
        {"Key": "p/b.txt", "Size": 10 * 1024 * 1024},
    ]
    with pytest.raises(Exception, match="CopyFailed"):
        ai._multipart_copy_concat(s3, "src", parts, "dest", "manifest.csv")

    s3.abort_multipart_upload.assert_called_once_with(
        Bucket="dest", Key="manifest.csv", UploadId="up-err",
    )


# ---------------------------------------------------------------------------
# _download_and_upload
# ---------------------------------------------------------------------------


def test_download_and_upload():
    s3 = MagicMock()
    body1 = MagicMock()
    body1.read.return_value = b"line1\n"
    body2 = MagicMock()
    body2.read.return_value = b"line2\n"
    s3.get_object.side_effect = [{"Body": body1}, {"Body": body2}]
    s3.put_object.return_value = {"ETag": '"uploaded"'}

    parts = [
        {"Key": "p/a.txt", "Size": 6},
        {"Key": "p/b.txt", "Size": 6},
    ]
    etag, total = ai._download_and_upload(s3, "src", parts, "dest", "manifest.csv")
    assert etag == '"uploaded"'
    assert total == 12
    s3.put_object.assert_called_once()
    assert s3.put_object.call_args.kwargs["Body"] == b"line1\nline2\n"


# ---------------------------------------------------------------------------
# _read_count_result — edge cases
# ---------------------------------------------------------------------------


def test_read_count_result_empty_body():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b""
    s3.get_object.return_value = {"Body": body}
    assert ai._read_count_result(s3, "s3://bucket/result.csv") == 0


def test_read_count_result_header_only():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b'"cnt"\n'
    s3.get_object.return_value = {"Body": body}
    assert ai._read_count_result(s3, "s3://bucket/result.csv") == 0


def test_read_count_result_malformed_value():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b'"cnt"\n"not_a_number"\n'
    s3.get_object.return_value = {"Body": body}
    assert ai._read_count_result(s3, "s3://bucket/result.csv") == 0


def test_read_count_result_unquoted():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b"cnt\n42\n"
    s3.get_object.return_value = {"Body": body}
    assert ai._read_count_result(s3, "s3://bucket/result.csv") == 42


# ---------------------------------------------------------------------------
# _wait_for_athena_query
# ---------------------------------------------------------------------------


def test_wait_for_athena_query_succeeded():
    athena = MagicMock()
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    ai._wait_for_athena_query(athena, "qid-1")  # should not raise


def test_wait_for_athena_query_failed():
    athena = MagicMock()
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "FAILED", "StateChangeReason": "syntax error"}
        }
    }
    with pytest.raises(RuntimeError, match="FAILED.*syntax error"):
        ai._wait_for_athena_query(athena, "qid-2")


def test_wait_for_athena_query_timeout():
    athena = MagicMock()
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "RUNNING"}}
    }
    with patch("nzshm_backup.athena_inventory.time") as mock_time:
        mock_time.time.side_effect = [0, 0, 1000]  # start, check, past deadline
        mock_time.sleep = MagicMock()
        with pytest.raises(TimeoutError, match="timed out"):
            ai._wait_for_athena_query(athena, "qid-3", timeout_seconds=10)


# ---------------------------------------------------------------------------
# _run_athena_query
# ---------------------------------------------------------------------------


def test_run_athena_query_with_database():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-99"}
    result = ai._run_athena_query(
        athena, "SELECT 1", "s3://output/", database="mydb"
    )
    assert result == "qid-99"
    call_kwargs = athena.start_query_execution.call_args.kwargs
    assert call_kwargs["QueryExecutionContext"] == {"Database": "mydb"}


def test_run_athena_query_without_database():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-100"}
    result = ai._run_athena_query(athena, "SELECT 1", "s3://output/")
    assert result == "qid-100"
    call_kwargs = athena.start_query_execution.call_args.kwargs
    assert "QueryExecutionContext" not in call_kwargs


# ---------------------------------------------------------------------------
# sample_objects_via_inventory
# ---------------------------------------------------------------------------


def test_sample_objects_via_inventory():
    """Full integration test with all AWS calls mocked."""
    s3 = MagicMock()
    athena = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456"}

    session = MagicMock()
    session.client.side_effect = lambda svc, **kw: {
        "s3": s3, "athena": athena, "sts": sts,
    }[svc]

    # Athena result CSV
    csv_data = (
        b'"key","e_tag","size"\n'
        b'"data/file1.txt","abc123",1024\n'
        b'"data/file2.txt","def456",2048\n'
    )
    result_body = MagicMock()
    result_body.read.return_value = csv_data
    s3.get_object.return_value = {"Body": result_body}

    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "ResultConfiguration": {
                "OutputLocation": "s3://ctrl/athena-results/result.csv"
            }
        }
    }

    with patch.object(
        ai, "_latest_inventory_partition",
        return_value=("2026-04-25-01-00", "inventory/backup/hive/"),
    ):
        with patch.object(ai, "_ensure_inventory_table"):
            with patch.object(ai, "_ensure_partition"):
                with patch.object(ai, "_run_athena_query", return_value="sample-qid"):
                    with patch.object(ai, "_wait_for_athena_query"):
                        objects = ai.sample_objects_via_inventory(
                            session, "ths", "backup-bucket", sample_size=2,
                        )

    assert len(objects) == 2
    assert objects[0]["Key"] == "data/file1.txt"
    assert objects[0]["ETag"] == '"abc123"'
    assert objects[0]["Size"] == 1024
    assert objects[1]["Key"] == "data/file2.txt"


def test_sample_objects_via_inventory_no_partitions():
    """Should raise ValueError when no inventory partitions exist."""
    s3 = MagicMock()
    athena = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456"}

    session = MagicMock()
    session.client.side_effect = lambda svc, **kw: {
        "s3": s3, "athena": athena, "sts": sts,
    }[svc]

    with patch.object(
        ai, "_latest_inventory_partition",
        side_effect=ValueError("No inventory data"),
    ):
        with pytest.raises(ValueError, match="No inventory data"):
            ai.sample_objects_via_inventory(
                session, "ths", "backup-bucket", sample_size=5,
            )


# ---------------------------------------------------------------------------
# _latest_inventory_partition — no data
# ---------------------------------------------------------------------------


def test_latest_inventory_partition_raises_when_empty():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
    with pytest.raises(ValueError, match="No inventory data"):
        ai._latest_inventory_partition(s3, "ctrl-bucket", "inventory/prefix")


# ---------------------------------------------------------------------------
# build_inventory_manifest_via_athena — zero row count
# ---------------------------------------------------------------------------


def test_build_inventory_manifest_zero_rows():
    """When row_count is 0, should return None etag and clean up."""
    s3 = MagicMock()
    athena = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456"}

    session = MagicMock()
    session.client.side_effect = lambda svc, **kw: {
        "s3": s3, "athena": athena, "sts": sts,
    }[svc]

    # count result returns 0
    count_body = MagicMock()
    count_body.read.return_value = b'"cnt"\n"0"\n'
    s3.get_object.return_value = {"Body": count_body}
    # no unload parts to clean
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]

    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "ResultConfiguration": {
                "OutputLocation": "s3://ctrl/athena-results/count.csv"
            }
        }
    }

    with patch.object(
        ai, "_latest_inventory_partition",
        side_effect=[
            ("2026-04-25-01-00", "inv/source/hive/"),
            ("2026-04-25-01-00", "inv/backup/hive/"),
        ],
    ):
        with patch.object(ai, "_ensure_inventory_table"):
            with patch.object(ai, "_ensure_partition"):
                with patch.object(
                    ai, "_run_athena_query",
                    side_effect=["unload-qid", "count-qid"],
                ):
                    with patch.object(ai, "_wait_for_athena_query"):
                        etag, src_dt, bkp_dt, count = (
                            ai.build_inventory_manifest_via_athena(
                                session, "ths", "src-bucket", "backup-bucket",
                                manifest_bucket="backup-bucket",
                                manifest_key="_manifests/test.csv",
                            )
                        )

    assert etag is None
    assert count == 0
    assert src_dt == "2026-04-25-01-00"
