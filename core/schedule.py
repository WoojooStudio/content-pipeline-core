"""Manual publish date — write-back of scheduled_for in content.json.

**Not automatic slot assignment.** `core/slots.py::assign_next_slot` is a separate feature that picks
the next free slot from the cadence (CLI `assign-slot`); this module records **only a time a human chose**
(date and time stay manual). The plan & queue screen and the CLI share this function.

Contract (same shape as publish.py's write-back contract):
- resolve IDs by scanning the source-of-truth files — don't trust index.json; also check the id inside the file
- **gate2_ok enforced** — the schema defines scheduled_for as "written after gate2_ok". Dating an unapproved
  post would put pre-gate items into the publish queue
- **published content is immutable** — editing the plan of a shipped post would distort history
- reject exited states (rejected/cancelled/failed)
- idempotent — same value leaves the file untouched, changed=False
- atomic write (tmp→os.replace) → one ledger line only when changed

**Setting a date does not publish anything.** scheduled_for is a planning field; publishing is still
done only by mark_published (human) or the gate-enforced publisher — this is not a gate bypass.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ledger import record as record_ledger

CNT_RE = re.compile(r"^CNT-[0-9]{3}-[0-9]{4}$")

# KST = UTC+9 — interpretation for tz-less input (same as slots.py)
KST = timezone(timedelta(hours=9))

_EXIT_KEYS = ("rejected_at", "cancelled_at", "failed_at")


class ScheduleRejected(Exception):
    """Schedule contract violation (ID format, not found, before gate, published, time format)."""


def set_schedule(
    content_id: str,
    channels_root: Path,
    when: str,
    *,
    ledger_path: Path | None = None,
) -> tuple[str, bool]:
    """Write scheduled_for to content.json (a human-chosen time).

    when: ISO 8601 string. Without tz it is read as KST and stored with an offset.
          Past times are allowed — sometimes a past slot is recorded; warnings are the UI's job.
    Returns: (stored scheduled_for, changed)
    """
    cnt_json, content = _load(content_id, channels_root)
    lifecycle = content.get("lifecycle", {})
    _require_schedulable(content_id, lifecycle)

    normalized = _normalize(when)
    if content.get("scheduled_for") == normalized:
        return normalized, False  # idempotent

    content["scheduled_for"] = normalized
    _atomic_write(cnt_json, content)
    _record(
        ledger_path, content_id, content,
        event="content.scheduled",
        meta={"scheduled_for": normalized},
    )
    return normalized, True


def clear_schedule(
    content_id: str,
    channels_root: Path,
    *,
    ledger_path: Path | None = None,
) -> tuple[None, bool]:
    """Clear scheduled_for (None). Rejects published/exited content — same as set_schedule."""
    cnt_json, content = _load(content_id, channels_root)
    lifecycle = content.get("lifecycle", {})
    _require_schedulable(content_id, lifecycle)

    if content.get("scheduled_for") is None:
        return None, False  # idempotent

    content["scheduled_for"] = None
    _atomic_write(cnt_json, content)
    _record(ledger_path, content_id, content, event="content.unscheduled", meta=None)
    return None, True


# ---------------------------------------------------------------------------


def _require_schedulable(content_id: str, lifecycle: dict) -> None:
    """Whether the date may be changed — enforces gate, exit and published rules (no bypass)."""
    for exit_key in _EXIT_KEYS:
        if lifecycle.get(exit_key):
            raise ScheduleRejected(
                f"{content_id} is in state {exit_key} — cannot set a date"
            )
    if not lifecycle.get("gate2_ok_at"):
        raise ScheduleRejected(
            f"{content_id} is before gate2_ok — cannot set a date (gate enforced)"
        )
    if lifecycle.get("published_at"):
        raise ScheduleRejected(
            f"{content_id} is already published — plan changes after publishing are rejected"
        )


def _normalize(when: str) -> str:
    """ISO 8601 → ISO 8601 with offset. Without tz, read as KST."""
    if not isinstance(when, str) or not when.strip():
        raise ScheduleRejected("Publish time is empty (ISO 8601 required)")
    raw = when.strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ScheduleRejected(f"Not ISO 8601: {when!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.isoformat()


def _load(content_id: str, channels_root: Path) -> tuple[Path, dict]:
    if not CNT_RE.match(content_id):
        raise ScheduleRejected(f"Invalid content ID format: {content_id!r}")
    cnt_json = _resolve_content_path(content_id, channels_root)
    if cnt_json is None:
        raise ScheduleRejected(f"Content not found: {content_id}")
    return cnt_json, json.loads(cnt_json.read_text(encoding="utf-8"))


def _resolve_content_path(content_id: str, channels_root: Path) -> Path | None:
    """Find content.json by scanning source-of-truth files (don't trust index.json; same as publish.py).

    Not importing publish.py's helper avoids a core→dashboard layering inversion
    (the same reasoning as publish.py inlining _draft_sha).
    """
    if not channels_root.exists():
        return None
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
            continue
        candidate = ch_dir / "content" / content_id / "content.json"
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if data.get("id") != content_id:
                raise ScheduleRejected(
                    f"content.json id ({data.get('id')}) does not match the directory/request ({content_id})"
                )
            return candidate
    return None


def _record(
    ledger_path: Path | None,
    content_id: str,
    content: dict,
    *,
    event: str,
    meta: dict | None,
) -> None:
    if ledger_path is None:
        return
    record_ledger(
        ledger_path,
        event=event,
        content_id=content_id,
        channel_id=content.get("channel_id"),
        module="dashboard",
        meta=meta,
    )


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
