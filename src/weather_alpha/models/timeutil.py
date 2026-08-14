"""Timezone-aware UTC helpers.

Naive datetimes are rejected. All stored timestamps in this project must be
timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any


class TimestampParseError(ValueError):
    """Raised when a timestamp cannot be parsed into aware UTC."""


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware; naive values are rejected")
    return value.astimezone(UTC)


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, bool):
        raise TimestampParseError("boolean is not a timestamp")
    if isinstance(value, int | float):
        return _epoch_to_utc(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TimestampParseError("empty timestamp string")
        if text.isdigit() or _is_numeric(text):
            return _epoch_to_utc(float(text))
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise TimestampParseError(f"unrecognized timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            # ISO date-only or naive datetime: treat date-only as UTC midnight,
            # reject clock times without an offset.
            if len(text) <= 10 and "T" not in text and " " not in text:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                raise TimestampParseError(f"timestamp is missing timezone offset: {value!r}")
        return ensure_utc(parsed)
    raise TimestampParseError(f"unsupported timestamp type: {type(value)!r}")


def _epoch_to_utc(value: float) -> datetime:
    # Unix seconds today are ~1e9; CLOB book timestamps are commonly ms (~1e12).
    if abs(value) >= 1e11:
        value = value / 1000.0
    return datetime.fromtimestamp(value, tz=UTC)


def _is_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


UTC_TZ: timezone = UTC
