"""policy — decides what to enforce per publishing mode (service does the enforcing).

policy = per-mode Gate2 / signature / nonce rules. This module reads profile.json and only
decides "can this channel auto-publish, and is a signature required"; it never checks gates
or writes files itself (that is service's job).

- auto_publish=false → no automatic publishing (manual channel). service rejects.
- auto_publish=true  → Gate2 + signed sidecar + nonce are enforced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class PolicyError(Exception):
    """Profile failed to load, or the policy could not be determined."""


@dataclass(frozen=True)
class PublishPolicy:
    channel_id: str
    platform: str
    auto_publish: bool
    credentials_ref: str
    require_signature: bool  # True when auto_publish=true

    @property
    def allows_auto_publish(self) -> bool:
        return self.auto_publish


def load_policy(channel_id: str, channels_root: Path) -> PublishPolicy:
    """Scan channel profile.json files and build the publishing policy."""
    profile = _find_profile(channel_id, channels_root)
    if profile is None:
        raise PolicyError(f"Channel profile not found: {channel_id}")
    auto = bool(profile.get("auto_publish", False))
    return PublishPolicy(
        channel_id=channel_id,
        platform=profile.get("platform", "threads"),
        auto_publish=auto,
        credentials_ref=profile.get("credentials_ref", ""),
        require_signature=auto,  # signatures are enforced only when auto_publish=true
    )


def _find_profile(channel_id: str, channels_root: Path) -> dict | None:
    if not channels_root.exists():
        return None
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
            continue
        profile_path = ch_dir / "profile.json"
        if not profile_path.exists():
            continue
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        if data.get("channel_id") == channel_id:
            return data
    return None
