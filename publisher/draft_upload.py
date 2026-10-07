"""draft_upload — the only entry point for uploading blog drafts to a platform (not publishing).

**Why separate from publish().** For Blogger the contract is "draft upload is automatic,
making it public is human-only". A draft on Blogger is **not a publish** — stamping
`published_at` here would make our "published" record diverge from what is actually live. So:

- `lifecycle` is **not touched** (published_at / publishing_at unchanged).
- The upload result is stored only as `platform_id` in `outputs[]` (content.json schema unchanged).
- The ledger gets `content.draft_uploaded` — a different name from the publish event.
- When a human runs `mark-published` after going live, the publish record is created then.

Gates are enforced the same as for publishing — unapproved posts never reach the platform.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from core.approval import approval_problem

from . import adapters
from .artifact import build_artifact
from .policy import load_policy
from .service import PublishBlocked, PublisherError

CNT_RE = re.compile(r"^CNT-[0-9]{3}-[0-9]{4}$")

_EXIT_KEYS = ("rejected_at", "cancelled_at", "failed_at")

# Platforms that support draft upload — others do not use this path.
_DRAFT_PLATFORMS = ("blogger",)


class DraftUploadResult:
    """Upload result. changed=False means an idempotent re-run (already uploaded)."""

    def __init__(self, content_id: str, changed: bool, post_id: str | None,
                 url: str | None, payload_hash: str) -> None:
        self.content_id = content_id
        self.changed = changed
        self.post_id = post_id
        self.url = url
        self.payload_hash = payload_hash

    def public(self) -> dict:
        return {
            "content_id": self.content_id, "changed": self.changed,
            "post_id": self.post_id, "url": self.url,
            "payload_hash": self.payload_hash,
        }


def upload_draft(
    content_id: str,
    channels_root: Path,
    transport,
    *,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: str | None = None,
) -> DraftUploadResult:
    """Upload the draft of gate2-approved content as a platform draft (never made public).

    Returns DraftUploadResult. If already uploaded, returns the existing post_id with changed=False.
    """
    if not CNT_RE.match(content_id):
        raise PublisherError(f"Invalid content ID format: {content_id!r}")

    cnt_json = _resolve(content_id, channels_root)
    if cnt_json is None:
        raise PublisherError(f"Content not found: {content_id}")

    content = json.loads(cnt_json.read_text(encoding="utf-8"))
    channel_id = content.get("channel_id")
    policy = load_policy(channel_id, channels_root)

    if policy.platform not in _DRAFT_PLATFORMS:
        raise PublisherError(
            f"{policy.platform} does not use the draft upload path "
            f"(supported: {', '.join(_DRAFT_PLATFORMS)})"
        )

    lifecycle = content.get("lifecycle", {})
    for exit_key in _EXIT_KEYS:
        if lifecycle.get(exit_key):
            raise PublishBlocked(f"{content_id} is in {exit_key} state — cannot upload")
    if not lifecycle.get("gate2_ok_at"):
        raise PublishBlocked(
            f"{content_id} has not passed gate2_ok — cannot upload draft (gate enforced)"
        )

    artifact = build_artifact(content, policy.platform)
    adapter = adapters.for_platform(policy.platform)

    # Idempotent — do not re-upload if already on this platform.
    existing = _existing_platform_id(content)
    if existing:
        return DraftUploadResult(content_id, False, existing, None, artifact.payload_hash)

    # Approval digest check — never upload a body changed after approval (same rule as mark_published).
    problem = approval_problem(content, cnt_json.parent)
    if problem:
        raise PublishBlocked(f"{content_id}: {problem}")
    # This rail's payload carries the body text only (artifact) — sending only the text of an
    # approved post with images would differ from what was approved. Image posts go out by hand via publish-pack.
    if (content.get("production") or {}).get("assets"):
        raise PublishBlocked(f"{content_id}: post has images — this rail sends text only. Use publish-pack")

    if dry_run:
        return DraftUploadResult(content_id, False, None, None, artifact.payload_hash)

    request = adapter.build_request(artifact.payload)
    resp = transport.send(request)
    post_id, url = adapter.parse_response(resp)

    # write-back — **lifecycle is not touched.** A draft upload is not a publish.
    outputs = content.setdefault("outputs", [])
    outputs.append({
        "lang": artifact.payload["lang"],
        "mode": artifact.payload["mode"],
        "file": None,
        "platform_id": post_id,
    })
    _atomic_write(cnt_json, content)

    if ledger_path is not None:
        _record_ledger(ledger_path, content_id, channel_id, post_id, url, now)

    return DraftUploadResult(content_id, True, post_id, url, artifact.payload_hash)


def _existing_platform_id(content: dict) -> str | None:
    for out in content.get("outputs", []):
        if out.get("platform_id"):
            return out["platform_id"]
    return None


def _resolve(content_id: str, channels_root: Path) -> Path | None:
    if not channels_root.exists():
        return None
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
            continue
        candidate = ch_dir / "content" / content_id / "content.json"
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if data.get("id") != content_id:
                raise PublisherError(
                    f"content.json id ({data.get('id')}) does not match request ({content_id})"
                )
            return candidate
    return None


def _record_ledger(ledger_path: Path, content_id, channel_id, post_id, url, now) -> None:
    """`content.draft_uploaded` — recorded under a different name from content.published."""
    from core.ledger import record as record_ledger

    record_ledger(
        ledger_path,
        event="content.draft_uploaded",
        content_id=content_id,
        channel_id=channel_id,
        module="publisher",
        meta={"platform_id": post_id, "url": url or None,
              "at": now or datetime.now(timezone.utc).isoformat()},
    )


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
