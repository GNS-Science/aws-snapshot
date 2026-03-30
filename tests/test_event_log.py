"""Tests for the append-only JSONL event log."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from nzshm_backup.event_log import append_event, read_events, _event_key

REGION = "ap-southeast-2"
BUCKET = "test-backup-bucket"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def s3_session():
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        session.client("s3").create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield session


# ---------------------------------------------------------------------------
# _event_key
# ---------------------------------------------------------------------------

def test_event_key_format():
    dt = datetime(2026, 3, 25, 7, 50, tzinfo=timezone.utc)
    assert _event_key(dt) == "_events/2026-03/events.jsonl"


def test_event_key_december():
    dt = datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert _event_key(dt) == "_events/2025-12/events.jsonl"


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------

class TestAppendEvent:
    def test_creates_new_file(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {"status": "started"})
        s3 = s3_session.client("s3")
        now = datetime.now(timezone.utc)
        key = _event_key(now)
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        event = json.loads(body.strip())
        assert event["event_type"] == "backup_run"
        assert event["source"] == "toshi"
        assert event["details"] == {"status": "started"}
        assert "timestamp" in event

    def test_appends_to_existing(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {"n": 1})
        append_event(s3_session, BUCKET, "backup_run_complete", "toshi", {"n": 2})
        s3 = s3_session.client("s3")
        key = _event_key(datetime.now(timezone.utc))
        lines = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "backup_run"
        assert json.loads(lines[1])["event_type"] == "backup_run_complete"

    def test_includes_actor_when_provided(self, s3_session):
        append_event(s3_session, BUCKET, "restore_submitted", "toshi", {}, actor="cli-user")
        s3 = s3_session.client("s3")
        key = _event_key(datetime.now(timezone.utc))
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        event = json.loads(body.strip())
        assert event["actor"] == "cli-user"

    def test_no_actor_field_when_not_provided(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {})
        s3 = s3_session.client("s3")
        key = _event_key(datetime.now(timezone.utc))
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        event = json.loads(body.strip())
        assert "actor" not in event

    def test_non_fatal_on_missing_bucket(self, s3_session):
        """Write to non-existent bucket should log a warning, not raise."""
        append_event(s3_session, "no-such-bucket", "backup_run", "toshi", {})
        # No exception raised

    def test_non_fatal_on_s3_error(self, s3_session):
        """Unexpected S3 errors should be swallowed with a warning."""
        from botocore.exceptions import ClientError

        def _raise(*args, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "GetObject")

        with patch.object(s3_session.client("s3").__class__, "get_object", _raise):
            append_event(s3_session, BUCKET, "backup_run", "toshi", {})
        # No exception raised

    def test_timestamp_is_utc_iso(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {})
        s3 = s3_session.client("s3")
        key = _event_key(datetime.now(timezone.utc))
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        event = json.loads(body.strip())
        dt = datetime.fromisoformat(event["timestamp"])
        assert dt.tzinfo is not None
        # Should be close to now
        delta = abs((dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
        assert delta < 5


# ---------------------------------------------------------------------------
# read_events
# ---------------------------------------------------------------------------

class TestReadEvents:
    def _write_raw(self, s3_session, key: str, events: list[dict]):
        s3 = s3_session.client("s3")
        body = "\n".join(json.dumps(e) for e in events) + "\n"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode())

    def test_reads_current_month(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {})
        events = read_events(s3_session, BUCKET)
        assert len(events) == 1
        assert events[0]["event_type"] == "backup_run"

    def test_returns_most_recent_first(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {"seq": 1})
        append_event(s3_session, BUCKET, "backup_run_complete", "toshi", {"seq": 2})
        events = read_events(s3_session, BUCKET)
        assert events[0]["event_type"] == "backup_run_complete"

    def test_filters_by_source(self, s3_session):
        append_event(s3_session, BUCKET, "backup_run", "toshi", {})
        append_event(s3_session, BUCKET, "backup_run", "ths", {})
        events = read_events(s3_session, BUCKET, source="toshi")
        assert all(e["source"] == "toshi" for e in events)
        assert len(events) == 1

    def test_respects_limit(self, s3_session):
        for i in range(10):
            append_event(s3_session, BUCKET, "backup_run", "toshi", {"i": i})
        events = read_events(s3_session, BUCKET, limit=3)
        assert len(events) == 3

    def test_empty_bucket_returns_empty_list(self, s3_session):
        events = read_events(s3_session, BUCKET)
        assert events == []

    def test_missing_bucket_returns_empty_list(self, s3_session):
        events = read_events(s3_session, "no-such-bucket")
        assert events == []

    def test_filters_by_since(self, s3_session):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        # Write one event with a past timestamp manually
        old_ts = (now - timedelta(hours=2)).isoformat()
        key = _event_key(now)
        s3 = s3_session.client("s3")
        old_event = {"event_type": "backup_run", "source": "toshi", "timestamp": old_ts, "details": {}}
        s3.put_object(Bucket=BUCKET, Key=key, Body=(json.dumps(old_event) + "\n").encode())
        # Append a fresh event
        append_event(s3_session, BUCKET, "backup_run_complete", "toshi", {})
        events = read_events(s3_session, BUCKET, since=cutoff)
        assert all(e["event_type"] == "backup_run_complete" for e in events)

    def test_scans_previous_month(self, s3_session):
        now = datetime.now(timezone.utc)
        prev = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
        prev_key = _event_key(prev)
        old_event = {
            "event_type": "backup_run",
            "source": "toshi",
            "timestamp": prev.isoformat(),
            "details": {},
        }
        s3 = s3_session.client("s3")
        s3.put_object(Bucket=BUCKET, Key=prev_key, Body=(json.dumps(old_event) + "\n").encode())
        events = read_events(s3_session, BUCKET)
        assert any(e["event_type"] == "backup_run" for e in events)

    def test_skips_malformed_lines(self, s3_session):
        key = _event_key(datetime.now(timezone.utc))
        s3 = s3_session.client("s3")
        good = json.dumps({"event_type": "backup_run", "source": "toshi",
                           "timestamp": datetime.now(timezone.utc).isoformat(), "details": {}})
        body = "not-json\n" + good + "\n   \n"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode())
        events = read_events(s3_session, BUCKET)
        assert len(events) == 1
        assert events[0]["event_type"] == "backup_run"
