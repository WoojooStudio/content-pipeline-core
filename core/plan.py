"""plan.json — per-channel publishing cadence targets.

`channels/<CH>/plan.json`. A small file holding only targets, so the content.json/profile.json
schemas stay untouched. **Missing means 'no target set'** — the file is not required (a channel
need not set targets as soon as it opens).

Writes are schema-validated and atomic. The plan & queue screen reads these to show "progress vs target".
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from jsonschema import ValidationError, validate

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "plan_schema.json"

DEFAULT_PLAN: dict = {
    "schema_version": 1,
    "weekly_cadence_target": None,
    "monthly_target": None,
    "notes": None,
}


class PlanError(Exception):
    """plan.json contract violation (schema failure, parse failure)."""


def plan_path(channel_dir: Path) -> Path:
    return channel_dir / "plan.json"


def load_plan(channel_dir: Path) -> dict:
    """Read a channel's plan.json. If missing, a copy of DEFAULT_PLAN (no target set).

    A broken file is not silently skipped — misreading the target as 0 would make progress lie.
    """
    p = plan_path(channel_dir)
    if not p.exists():
        return dict(DEFAULT_PLAN)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise PlanError(f"Cannot read plan.json: {p} ({e})") from e
    _validate(data, p)
    # Fill missing keys with defaults so consumers need no .get branching.
    merged = dict(DEFAULT_PLAN)
    merged.update(data)
    return merged


def write_plan(channel_dir: Path, plan: dict) -> dict:
    """Validate plan.json against the schema and write it atomically. Returns the written dict."""
    data = dict(DEFAULT_PLAN)
    data.update(plan or {})
    data["schema_version"] = 1
    p = plan_path(channel_dir)
    _validate(data, p)
    if not channel_dir.exists():
        raise PlanError(f"Channel directory does not exist: {channel_dir}")
    _atomic_write(p, data)
    return data


def _validate(data: dict, path: Path) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        validate(instance=data, schema=schema)
    except ValidationError as e:
        raise PlanError(f"plan.json schema violation ({path}): {e.message}") from e


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
