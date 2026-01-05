"""UTC timestamp boundaries for persisted application records."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc_timestamp(value: Any = None) -> str | None:
    """Return ISO-8601 UTC; legacy numeric recovery times are normalised here."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
        else:
            return (
                parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if parsed.tzinfo is not None
                else None
            )
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp >= 1e18:
        timestamp /= 1e9
    elif timestamp >= 1e15:
        timestamp /= 1e6
    elif timestamp >= 1e12:
        timestamp /= 1e3
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def utc_now_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
