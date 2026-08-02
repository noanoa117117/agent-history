"""UTC timestamp helpers used by the database and CLI."""

from __future__ import annotations

from datetime import date, datetime, time, timezone


UTC = timezone.utc


def utc_now_iso() -> str:
    """Return the current time as a timezone-aware UTC ISO 8601 string."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def parse_timestamp(value: str, *, label: str = "timestamp") -> str:
    """Parse an aware ISO timestamp and normalize it to UTC."""

    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone, for example +00:00 or Z")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def parse_filter_bound(value: str, *, end: bool = False, label: str = "date") -> str:
    """Parse a date or timestamp for a UTC search boundary.

    Date-only values cover the complete UTC day. A naive timestamp is accepted
    for filters and interpreted as UTC; persisted timestamps are never naive.
    """

    text = value.strip()
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        boundary_time = time.max if end else time.min
        return datetime.combine(parsed_date, boundary_time, tzinfo=UTC).isoformat(
            timespec="microseconds"
        )

    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")
