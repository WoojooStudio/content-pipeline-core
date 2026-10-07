"""Content-file helpers shared by the publisher.

Extracted for this public snapshot from a dashboard module in the private codebase,
so the publisher can run on its own.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CNT_RE = re.compile(r"^CNT-[0-9]{3}-[0-9]{4}$")


class ContentPathError(Exception):
    """A content.json was found but its id disagrees with its folder or the request."""


def _resolve_content_path(content_id: str, channels_root: Path) -> Path | None:
    """Scan the source-of-truth files for content.json (the derived index is not trusted).

    Checks that the id inside the file matches the requested id and the folder name.
    """
    if not channels_root.exists():
        return None
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
            continue
        candidate = ch_dir / "content" / content_id / "content.json"
        if candidate.exists():
            data = json.loads(candidate.read_text())
            if data.get("id") != content_id:
                raise ContentPathError(
                    f"content.json id ({data.get('id')}) does not match folder/request ({content_id})"
                )
            return candidate
    return None


def _atomic_write(path: Path, data: dict) -> None:
    # Unique temp name per process, so concurrent runs never overwrite each other's temp file.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
