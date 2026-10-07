"""CNT numbering and scaffolding — 4-digit sequence per channel, collisions avoided by directory existence checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .gate import record_gate, submit_gate
from .ledger import record as record_ledger


def scaffold_cnt(
    channel_id: str,
    title: str,
    channels_root: Path,
    ledger_path: Path | None = None,
    gate0: dict | None = None,
) -> tuple[str, Path]:
    """Atomically allocate a CNT ID, create its directory and initialise content.json.

    Splitting numbering from mkdir opens a TOCTOU race, so this is the only entry point.
    mkdir(exist_ok=False) claims the directory — FileExistsError if it already exists.

    If ledger_path is given, a content.drafted event is written to the ledger
    (the starting point for content-unit lifecycle/ROI tracking).
    Drafting token cost is not measured, so cost_usd is omitted.

    If gate0 is given (dict: picked_at, picked_by, topic_batch_id, topic_uuid), stamp
    content.json gates.g0 + lifecycle to show this content came from a Gate0 topic
    pick (full traceability). gate0_ok_at inherits the pick time
    (picked_at) so lead time is not distorted: gate0_ok_at ≤ drafted_at.
    If gate0 is None, g0 is not written (backward compatible — manual numbering without Gate0).

    Returns: (cnt_id, content_json_path)
    """
    ch_num = channel_id.split("-")[1]
    content_dir = _find_content_dir(channel_id, channels_root)

    if content_dir is None:
        raise ValueError(f"Channel directory not found: {channel_id} in {channels_root}")

    existing = _scan_existing(ch_num, content_dir)
    nxt = (max(existing) + 1) if existing else 1
    cnt_id = f"CNT-{ch_num}-{nxt:04d}"

    cnt_dir = content_dir / cnt_id
    cnt_dir.mkdir(parents=False, exist_ok=False)  # claim first — prevents races

    now = datetime.now(timezone.utc).isoformat()
    content = {
        "schema_version": 6,
        "id": cnt_id,
        "channel_id": channel_id,
        "title": title,
        "topic_uuid": gate0.get("topic_uuid") if gate0 else None,
        "scheduled_for": None,
        "experiment": None,
        "lifecycle": {
            "gate0_pending_at": None,
            "gate0_ok_at": None,
            "drafted_at": now,
            "gate1_pending_at": None,
            "gate1_ok_at": None,
            "producing_at": None,
            "rendering_at": None,
            "produced_at": None,
            "gate2_pending_at": None,
            "gate2_ok_at": None,
            "publishing_at": None,
            "published_at": None,
            "measured_at": None,
            "rejected_at": None,
            "failed_at": None,
            "cancelled_at": None,
        },
        "gates": {
            "g0": {"checks": {}, "by": None, "at": None, "provenance": None},
            "g1": {"checks": {}, "by": None, "at": None, "provenance": None},
            "g2": {"checks": {}, "by": None, "at": None, "provenance": None},
        },
        "production": {"brief": None, "draft": None, "human_minutes": 0, "fail_reason": None},
        "outputs": [],
        "jobs": [],
    }

    cnt_json = cnt_dir / "content.json"
    cnt_json.write_text(json.dumps(content, ensure_ascii=False, indent=2))

    # Gate0 stamp — inherit the pick time so gate0_ok_at ≤ drafted_at.
    # If it were later than drafted_at, status derivation could stick at gate0, so use min
    # (g0 is a stage before drafted; timestamps must not invert).
    if gate0 is not None:
        picked_at = gate0.get("picked_at") or now
        gate0_at = min(picked_at, now)
        submit_gate(cnt_json, "g0", gate0_at)
        provenance = {
            "topic_batch_id": gate0["topic_batch_id"],
            "topic_uuid": gate0["topic_uuid"],
            "picked_by": gate0.get("picked_by"),
        }
        record_gate(cnt_json, "g0", {"g0_topic_picked": True}, by="topic-apply",
                    at=gate0_at, provenance=provenance)

    if ledger_path is not None:
        record_ledger(
            ledger_path,
            event="content.drafted",
            content_id=cnt_id,
            channel_id=channel_id,
            module="core",
        )

    return cnt_id, cnt_json


def next_cnt(channel_id: str, channels_root: Path) -> str:
    """Return the next CNT ID only (no directory created). Read-only.

    Use scaffold_cnt to actually create one.
    """
    ch_num = channel_id.split("-")[1]
    content_dir = _find_content_dir(channel_id, channels_root)

    if content_dir is None or not content_dir.exists():
        return f"CNT-{ch_num}-0001"

    existing = _scan_existing(ch_num, content_dir)
    nxt = (max(existing) + 1) if existing else 1
    return f"CNT-{ch_num}-{nxt:04d}"


def _find_content_dir(channel_id: str, channels_root: Path) -> Path | None:
    for candidate in channels_root.iterdir():
        if candidate.is_dir() and candidate.name.startswith(channel_id):
            return candidate / "content"
    return None


# Where archived content units go — under the content directory. Index and publish paths skip it,
# but numbering does not: reusing an archived number would collide with old ledger/metrics records.
ARCHIVE_DIRNAME = "_archived"


def _scan_existing(ch_num: str, content_dir: Path) -> list[int]:
    prefix = f"CNT-{ch_num}-"
    nums = []
    for folder in (content_dir, content_dir / ARCHIVE_DIRNAME):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                try:
                    nums.append(int(p.name[len(prefix):]))
                except ValueError:
                    pass
    return nums
