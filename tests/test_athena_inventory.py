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
