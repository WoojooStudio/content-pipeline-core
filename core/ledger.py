"""ledger.jsonl — single source of truth for costs and events. Append-only."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def record(
    ledger_path: Path,
    *,
    event: str,
    content_id: str | None = None,
    channel_id: str | None = None,
    module: str | None = None,
    job_id: str | None = None,
    cost_usd: float | None = None,
    meta: dict | None = None,
) -> dict:
    """Append one event line to ledger.jsonl. Returns the written dict."""
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    if content_id is not None:
        entry["content_id"] = content_id
    if channel_id is not None:
        entry["channel_id"] = channel_id
    if module is not None:
        entry["module"] = module
    if job_id is not None:
        entry["job_id"] = job_id
    if cost_usd is not None:
        entry["cost_usd"] = cost_usd
    if meta is not None:
        entry["meta"] = meta

    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def read_all(ledger_path: Path) -> list[dict]:
    """Parse all of ledger.jsonl and return it as a list."""
    if not ledger_path.exists():
        return []
    entries = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries
