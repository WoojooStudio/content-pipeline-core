"""Timestamp → status derivation. lifecycle.*_at timestamps are the truth; status is a cache."""
from __future__ import annotations

from datetime import datetime, timezone

# Normal progress states — ascending (the latest timestamp is the current state)
_PROGRESS: list[tuple[str, str]] = [
    ("drafted_at",       "drafted"),
    ("gate1_pending_at", "gate1_pending"),
    ("gate1_ok_at",      "gate1_ok"),
    ("producing_at",     "producing"),
    ("rendering_at",     "rendering"),
    ("produced_at",      "produced"),
    ("gate2_pending_at", "gate2_pending"),
    ("gate2_ok_at",      "gate2_ok"),
    ("publishing_at",    "publishing"),
    ("published_at",     "published"),
    ("measured_at",      "measured"),
]

# Exit states — if several are set, the latest timestamp wins
_EXIT: list[tuple[str, str]] = [
    ("rejected_at",  "rejected"),
    ("cancelled_at", "cancelled"),
    ("failed_at",    "failed"),
]


def _parse_ts(ts: str) -> datetime:
    """Convert an ISO-8601 string to a UTC datetime. No offset means UTC."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def derive_status(lifecycle: dict) -> str:
    """Derive the current status from a lifecycle dict.

    Timestamps are the truth — when states overlap, the latest timestamp wins.
    Exit states (rejected/cancelled/failed) are compared by timestamp too.
    """
    best_ts: datetime | None = None
    best_status = "drafted"

    # latest progress state
    for ts_key, status in _PROGRESS:
        raw = lifecycle.get(ts_key)
        if not raw:
            continue
        dt = _parse_ts(raw)
        if best_ts is None or dt >= best_ts:
            best_ts = dt
            best_status = status

    # latest exit state — overrides progress if it is later
    for ts_key, status in _EXIT:
        raw = lifecycle.get(ts_key)
        if not raw:
            continue
        dt = _parse_ts(raw)
        if best_ts is None or dt >= best_ts:
            best_ts = dt
            best_status = status

    return best_status
