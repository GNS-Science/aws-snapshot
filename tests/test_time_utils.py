"""Tests for shared timezone / datetime parsing utilities."""

from datetime import timedelta, timezone

import pytest

from nzshm_backup.time_utils import TZ_ABBREV, parse_datetime

NZST = timezone(timedelta(hours=12))
NZDT = timezone(timedelta(hours=13))
UTC = timezone.utc


class TestParseDateTime:
    # --- ISO 8601 ---

    def test_iso8601_with_offset(self):
        dt = parse_datetime("2026-03-25T07:50:00+13:00")
        assert dt.utcoffset() == timedelta(hours=13)
        assert dt.hour == 7
        assert dt.minute == 50

    def test_iso8601_utc_z(self):
        dt = parse_datetime("2026-03-25T09:00:00+00:00")
        assert dt.tzinfo == UTC
        assert dt.hour == 9

    def test_iso8601_bare_assumed_utc(self):
        """Bare ISO datetime (no tz) is assumed UTC."""
        dt = parse_datetime("2026-03-25T09:00:00")
        assert dt.tzinfo == UTC
        assert dt.hour == 9

    def test_iso8601_date_with_space_separator(self):
        """datetime.fromisoformat accepts space as separator."""
        dt = parse_datetime("2026-03-25 09:00:00")
        assert dt.tzinfo == UTC

    # --- Display format: YYYY-MM-DD HH:MM TZ ---

    def test_display_format_nzdt(self):
        dt = parse_datetime("2026-03-25 07:50 NZDT")
        assert dt.utcoffset() == timedelta(hours=13)
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 25
        assert dt.hour == 7
        assert dt.minute == 50

    def test_display_format_nzst(self):
        dt = parse_datetime("2026-06-01 02:00 NZST")
        assert dt.utcoffset() == timedelta(hours=12)

    def test_display_format_utc(self):
        dt = parse_datetime("2026-03-25 14:00 UTC")
        assert dt.tzinfo == UTC
        assert dt.hour == 14

    def test_display_format_aest(self):
        dt = parse_datetime("2026-03-25 12:00 AEST")
        assert dt.utcoffset() == timedelta(hours=10)

    def test_display_format_aedt(self):
        dt = parse_datetime("2026-03-25 12:00 AEDT")
        assert dt.utcoffset() == timedelta(hours=11)

    # --- Time-only with TZ ---

    def test_time_only_nzdt(self):
        dt = parse_datetime("12:15 NZDT")
        assert dt.utcoffset() == timedelta(hours=13)
        assert dt.hour == 12
        assert dt.minute == 15
        # Anchored to year=2000 month=1 day=1
        assert dt.year == 2000

    def test_time_only_utc(self):
        dt = parse_datetime("02:00 UTC")
        assert dt.tzinfo == UTC
        assert dt.hour == 2

    # --- UTC conversion ---

    def test_nzdt_converts_correctly_to_utc(self):
        """12:15 NZDT = 23:15 UTC previous day."""
        dt = parse_datetime("2026-03-29 12:15 NZDT")
        dt_utc = dt.astimezone(UTC)
        assert dt_utc.hour == 23
        assert dt_utc.minute == 15
        assert dt_utc.day == 28  # previous day in UTC

    def test_nzst_converts_correctly_to_utc(self):
        """02:00 NZST = 14:00 UTC previous day."""
        dt = parse_datetime("2026-06-01 02:00 NZST")
        dt_utc = dt.astimezone(UTC)
        assert dt_utc.hour == 14
        assert dt_utc.day == 31  # May 31

    # --- Whitespace handling ---

    def test_leading_trailing_whitespace_stripped(self):
        dt = parse_datetime("  2026-03-25 07:50 NZDT  ")
        assert dt.hour == 7

    # --- Error cases ---

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Cannot parse timestamp"):
            parse_datetime("not-a-date")

    def test_unknown_tz_abbrev_raises(self):
        with pytest.raises(ValueError):
            parse_datetime("12:00 PST")  # PST not in TZ_ABBREV

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_datetime("")


class TestTzAbbrev:
    def test_all_expected_keys_present(self):
        for key in ("UTC", "NZST", "NZDT", "AEST", "AEDT"):
            assert key in TZ_ABBREV

    def test_utc_offset_values(self):
        assert TZ_ABBREV["UTC"] == UTC
        assert TZ_ABBREV["NZST"].utcoffset(None) == timedelta(hours=12)
        assert TZ_ABBREV["NZDT"].utcoffset(None) == timedelta(hours=13)
        assert TZ_ABBREV["AEST"].utcoffset(None) == timedelta(hours=10)
        assert TZ_ABBREV["AEDT"].utcoffset(None) == timedelta(hours=11)
