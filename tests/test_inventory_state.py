"""Tests for inventory_state freshness signals."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from aws_snapshot.inventory_state import _latest_object_ts


def _page(contents: list[dict]) -> dict:
    return {"Contents": contents}


def _mock_s3_with_pages(pages: list[dict]) -> MagicMock:
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    s3.get_paginator.return_value = paginator
    return s3


def test_latest_object_ts_picks_freshest_non_empty():
    """Freshest object with Size > 0 wins."""
    pages = [
        _page(
            [
                {
                    "Key": "old.parquet",
                    "Size": 1024,
                    "LastModified": datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc),
                },
                {
                    "Key": "newer.parquet",
                    "Size": 2048,
                    "LastModified": datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
                },
            ]
        )
    ]
    s3 = _mock_s3_with_pages(pages)
    ts = _latest_object_ts(s3, "bucket", "prefix")
    assert ts == datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc)


def test_latest_object_ts_skips_zero_byte_placeholders():
    """A 0-byte ``data/`` folder marker must not count as a fresh inventory.

    Regression for the toy-inv 2026-05-27 sandbox finding: setup-inventory
    (or AWS itself) can drop a 0-byte object at the destination prefix
    before any real inventory delivery, falsely making freshness look OK.
    """
    pages = [
        _page(
            [
                {
                    "Key": "real-data.parquet",
                    "Size": 4096,
                    "LastModified": datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc),
                },
                # Placeholder created at setup time — should be ignored
                {
                    "Key": "data/",
                    "Size": 0,
                    "LastModified": datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
                },
            ]
        )
    ]
    s3 = _mock_s3_with_pages(pages)
    ts = _latest_object_ts(s3, "bucket", "prefix")
    # The 0-byte placeholder is newer but excluded → falls back to the
    # real parquet's older timestamp.
    assert ts == datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)


def test_latest_object_ts_returns_none_when_only_placeholders():
    """All-placeholder prefix returns None so the freshness check reds."""
    pages = [
        _page(
            [
                {
                    "Key": "data/",
                    "Size": 0,
                    "LastModified": datetime(2026, 5, 27, 3, 10, tzinfo=timezone.utc),
                }
            ]
        )
    ]
    s3 = _mock_s3_with_pages(pages)
    ts = _latest_object_ts(s3, "bucket", "prefix")
    assert ts is None


def test_latest_object_ts_returns_none_on_empty_prefix():
    """No objects → None (which surfaces as 'no inventory data available')."""
    s3 = _mock_s3_with_pages([_page([])])
    ts = _latest_object_ts(s3, "bucket", "prefix")
    assert ts is None
