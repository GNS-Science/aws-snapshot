"""Tests for Athena-backed inventory manifest helpers."""

from unittest.mock import MagicMock, patch

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


def test_build_inventory_manifest_rows_via_athena_returns_encoded_manifest_rows():
    s3 = MagicMock()
    s3.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=b"key\nfolder/file 1.txt\nplain.txt\n"))
    }
    athena = MagicMock()
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "ResultConfiguration": {
                "OutputLocation": "s3://nzshm-backup-inventory-123/athena-results/q1.csv"
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

    run_query = MagicMock(return_value="q-123")
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
                        rows, source_dt, backup_dt = ai.build_inventory_manifest_rows_via_athena(
                            session,
                            "ths",
                            "ths-dataset-prod",
                            "bb-ths-s3-dataset-prod-ap-southeast-2-461564345538",
                            full_sync=False,
                        )

    assert source_dt == "2026-04-23-01-00"
    assert backup_dt == "2026-04-23-01-00"
    assert list(rows) == ["ths-dataset-prod,folder/file%201.txt\n", "ths-dataset-prod,plain.txt\n"]
    assert "LEFT JOIN" in run_query.call_args.args[1]
