"""Rebuild state/index.json — scans every channel's content.json to build a derived index."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .approval import requires_production
from .status import derive_status


def rebuild(channels_root: Path, index_path: Path) -> dict:
    """Scan the channels/ directory and rebuild state/index.json.

    Returns: the written index dict.
    """
    index: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channels": [],
    }

    if not channels_root.exists():
        _write(index_path, index)
        return index

    for ch_dir in sorted(channels_root.iterdir()):
        if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
            continue

        profile_path = ch_dir / "profile.json"
        if not profile_path.exists():
            continue

        profile = json.loads(profile_path.read_text())
        ch_platform = profile.get("platform")
        ch_format = profile.get("format")
        channel_entry = {
            "channel_id": profile.get("channel_id"),
            "name": profile.get("name"),
            "lifecycle_state": profile.get("lifecycle_state"),
            "platform": ch_platform,
            "format": ch_format,
            "contents": [],
        }

        content_dir = ch_dir / "content"
        if content_dir.exists():
            for cnt_dir in sorted(content_dir.iterdir()):
                if not cnt_dir.is_dir():
                    continue
                cnt_json = cnt_dir / "content.json"
                if not cnt_json.exists():
                    continue
                content = json.loads(cnt_json.read_text())
                lifecycle = content.get("lifecycle", {})
                status = derive_status(lifecycle)
                draft = (content.get("production") or {}).get("draft")
                experiment = content.get("experiment") or {}
                eff_format = _effective_format(content, experiment, ch_format, ch_platform)
                channel_entry["contents"].append({
                    "id": content.get("id"),
                    "title": content.get("title"),
                    "status": status,
                    "category": experiment.get("category"),
                    "body": draft,
                    "char_count": len(draft) if isinstance(draft, str) else 0,
                    "drafted_at": lifecycle.get("drafted_at"),
                    "scheduled_for": content.get("scheduled_for"),
                    "published_at": lifecycle.get("published_at"),
                    # Full lifecycle timestamps — the plan & queue pipeline view needs entry times
                    # to compute days spent per lane. This only adds a derived cache field;
                    # it does not touch the content.json schema and existing consumers ignore it.
                    "lifecycle": dict(lifecycle),
                    "platform": _effective_platform(content, ch_platform),
                    "effective_format": eff_format,
                    # Whether production must complete before Gate2 — same check as the approval CLI (core.approval).
                    # effective_format (the preview key) is not used since it may follow experiment free text.
                    "needs_production": requires_production(content, ch_format, ch_platform),
                    # Production output projection (content v6) — copy only ordered assets and the final caption
                    # for later previews. Paths stay relative to the content unit directory.
                    "media_kind": _MEDIA_KIND.get(eff_format or ""),
                    "caption": (content.get("production") or {}).get("caption"),
                    "assets": _ordered_assets(content),
                })

        index["channels"].append(channel_entry)

    _write(index_path, index)
    return index


# Default format per platform — used as the preview key when no explicit canonical format exists.
_DEFAULT_FORMAT_BY_PLATFORM = {
    "threads": "text",
    "instagram": "card",
    "youtube": "video",
    "blogger": "blog",
}

# Canonical format values the preview renderer understands. experiment.format etc. hold narrative
# free text and cannot be preview keys — honour known values only, otherwise use the platform default.
# reel must not fall back to the instagram default (card) — kept as a separate video-type canonical value.
_CANONICAL_FORMATS = {"text", "card", "carousel", "image", "video", "reel", "blog"}

# Free-text aliases → canonical values (common spellings in experiment.format etc.).
_FORMAT_ALIASES = {"reels": "reel"}

# Canonical format → media kind. Decides whether the preview draws image, video or text.
_MEDIA_KIND = {
    "text": "text", "blog": "text",
    "card": "image", "carousel": "image", "image": "image",
    "video": "video", "reel": "video",
}


def _effective_platform(content: dict, channel_platform: str | None) -> str | None:
    """Publishing platform for the content — falls back outputs → experiment → channel profile."""
    for out in content.get("outputs", []):
        if out.get("platform"):
            return out["platform"]
    exp = content.get("experiment") or {}
    return exp.get("platform") or channel_platform


def _effective_format(
    content: dict, experiment: dict, channel_format: str | None,
    channel_platform: str | None,
) -> str | None:
    """The format axis of the preview selection key.

    Fallback order: content.format → artifact.format → experiment.format → profile.format.
    Only canonical formats the preview renderer understands (_CANONICAL_FORMATS) are honoured —
    experiment.format etc. are narrative free text and cannot be preview keys.
    Without a canonical value, fall back to the platform default (threads→text, instagram→card).
    """
    for candidate in (
        content.get("format"),
        (content.get("artifact") or {}).get("format"),
        experiment.get("format"),
        channel_format,
    ):
        candidate = _FORMAT_ALIASES.get(candidate, candidate) if isinstance(candidate, str) else None
        if candidate in _CANONICAL_FORMATS:
            return candidate
    platform = _effective_platform(content, channel_platform)
    return _DEFAULT_FORMAT_BY_PLATFORM.get(platform or "", None)


# Public name — Gate2 approval uses the same fallback to decide "does this format need production".
def effective_format(content: dict, channel_format: str | None,
                     channel_platform: str | None) -> str | None:
    return _effective_format(content, content.get("experiment") or {},
                             channel_format, channel_platform)


def _ordered_assets(content: dict) -> list[dict]:
    """production.assets in order (file, role, order, sha256 only). Empty list if none."""
    assets = (content.get("production") or {}).get("assets") or []
    out = [
        {k: a.get(k) for k in ("file", "role", "order", "sha256")}
        for a in assets if isinstance(a, dict)
    ]
    return sorted(out, key=lambda a: a.get("order") or 0)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)  # atomic rename — keeps the old file on crash
