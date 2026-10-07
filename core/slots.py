"""Publish slot assignment — find the next free slot from the channel cadence and write it to content.json.

Only fixed cadence parsing for now: "3/week" → Tue/Thu/Sat 21:00 KST.
If profile.json has a publish_slots array, it takes precedence.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# KST = UTC+9
KST = timezone(timedelta(hours=9))

# 3/week default slots: Tuesday(1), Thursday(3), Saturday(5) 21:00 KST
_DEFAULT_3PER_WEEK: list[tuple[int, time]] = [
    (1, time(21, 0)),  # Tue
    (3, time(21, 0)),  # Thu
    (5, time(21, 0)),  # Sat
]


def assign_next_slot(
    content_id: str,
    channels_root: Path,
    *,
    after: datetime | None = None,
) -> str:
    """Assign the next free publish slot to the content and write it atomically to content.json.

    after: only assign slots after this time (default: now, KST). Injected in tests.
    Returns: the assigned slot as an ISO 8601 string (KST, e.g. "2026-07-29T21:00:00+09:00").

    Content that already has scheduled_for is idempotent — the existing value is returned.
    """
    cnt_path = _find_content_json(content_id, channels_root)
    content = json.loads(cnt_path.read_text(encoding="utf-8"))

    existing = content.get("scheduled_for")
    if existing:
        return existing

    profile = _load_profile(cnt_path)
    slots = _parse_slots(profile)
    taken = _collect_taken_slots(content_id, cnt_path.parent.parent, channels_root)

    ref = after if after is not None else datetime.now(KST)
    slot_dt = _next_available(slots, taken, ref)
    slot_str = slot_dt.isoformat()

    content["scheduled_for"] = slot_str
    _atomic_write(cnt_path, content)
    return slot_str


def _find_content_json(content_id: str, channels_root: Path) -> Path:
    """Find the content.json for content_id under the channels root."""
    ch_num = content_id.split("-")[1]
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir():
            continue
        if not ch_dir.name.startswith(f"CH-{ch_num}"):
            continue
        p = ch_dir / "content" / content_id / "content.json"
        if p.exists():
            return p
    raise FileNotFoundError(f"content.json not found for {content_id} under {channels_root}")


def _load_profile(cnt_path: Path) -> dict:
    """Load profile.json two levels up from the content.json path."""
    ch_dir = cnt_path.parent.parent.parent  # channels/<ch_slug>/
    profile_path = ch_dir / "profile.json"
    if profile_path.exists():
        return json.loads(profile_path.read_text(encoding="utf-8"))
    return {}


def _parse_slots(profile: dict) -> list[tuple[int, time]]:
    """profile.json → list of (weekday, time) slots.

    Uses the profile's publish_slots array if present:
      [{"weekday": 1, "hour": 21, "minute": 0}, ...]
    Otherwise the cadence "3/week" default: Tue/Thu/Sat 21:00 KST.
    """
    raw = profile.get("publish_slots")
    if raw and isinstance(raw, list):
        result = []
        for s in raw:
            wd = int(s["weekday"])  # 0=Mon … 6=Sun (Python weekday)
            t = time(int(s.get("hour", 21)), int(s.get("minute", 0)))
            result.append((wd, t))
        return result or _DEFAULT_3PER_WEEK
    return _DEFAULT_3PER_WEEK


def _collect_taken_slots(
    skip_id: str, content_dir: Path, channels_root: Path
) -> set[str]:
    """Collect scheduled_for values from all content in the same channel (excluding skip_id)."""
    taken: set[str] = set()
    if not content_dir.exists():
        return taken
    for cnt_dir in content_dir.iterdir():
        if not cnt_dir.is_dir() or cnt_dir.name == skip_id:
            continue
        p = cnt_dir / "content.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            slot = data.get("scheduled_for")
            if slot:
                taken.add(slot)
        except (json.JSONDecodeError, OSError):
            pass
    return taken


def _next_available(
    slots: list[tuple[int, time]],
    taken: set[str],
    after: datetime,
) -> datetime:
    """Return the first free slot after `after`, given the slot list and the already-assigned set."""
    ref_date = after.astimezone(KST).date()
    for delta_days in range(0, 90):  # search up to 90 days
        d = ref_date + timedelta(days=delta_days)
        for weekday, slot_time in sorted(slots, key=lambda s: s[0]):
            if d.weekday() != weekday:
                continue
            candidate = datetime.combine(d, slot_time, tzinfo=KST)
            if candidate <= after.astimezone(KST):
                continue
            iso = candidate.isoformat()
            if iso not in taken:
                return candidate
    raise RuntimeError("No free slot within 90 days — check the slot settings")


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
