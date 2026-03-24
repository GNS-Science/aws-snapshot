"""Shared timezone and datetime parsing utilities."""

from datetime import datetime, timedelta, timezone

# Offset lookup for common timezone abbreviations (matches _fmt_dt display output)
TZ_ABBREV: dict[str, timezone] = {
    "UTC":  timezone.utc,
    "NZST": timezone(timedelta(hours=12)),
    "NZDT": timezone(timedelta(hours=13)),
    "AEST": timezone(timedelta(hours=10)),
    "AEDT": timezone(timedelta(hours=11)),
}


def parse_datetime(ts: str) -> datetime:
    """Parse a timestamp string into an aware datetime.

    Accepts:
    - ISO 8601:            ``2026-03-25T07:50:00+13:00``
    - Display format:      ``2026-03-25 07:50 NZDT``
    - Time + abbreviation: ``07:50 NZDT``

    Bare datetimes or times with no timezone are assumed UTC.
    """
    ts = ts.strip()

    # Try ISO 8601 first (handles "2026-03-25T07:50:00+13:00" and "2026-03-25 07:50:00")
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Try "... TZ" suffix — works for "YYYY-MM-DD HH:MM TZ" and "HH:MM TZ"
    parts = ts.rsplit(" ", 1)
    if len(parts) == 2 and parts[1] in TZ_ABBREV:
        tz = TZ_ABBREV[parts[1]]
        body = parts[0].strip()
        # Try full datetime body first, then time-only
        for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):
            try:
                dt = datetime.strptime(body, fmt)
                if fmt == "%H:%M":
                    # Anchor to a fixed date — only the time component is used by callers
                    dt = dt.replace(year=2000, month=1, day=1)
                return dt.replace(tzinfo=tz)
            except ValueError:
                continue

    raise ValueError(
        f"Cannot parse timestamp {ts!r}. "
        "Accepted formats: ISO 8601 (e.g. '2026-03-25T07:50:00+13:00'), "
        "'YYYY-MM-DD HH:MM TZ', 'HH:MM TZ', or bare 'HH:MM' (assumed UTC). "
        f"Known timezone abbreviations: {', '.join(TZ_ABBREV)}."
    )
