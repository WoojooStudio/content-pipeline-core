"""artifact — normalizes content.json into the transmit payload and hashes it canonically.

The signature covers **the whole transmit payload** (caption, channel, platform destination,
language, mode), not just the text. If an adapter re-assembled the payload after verification
that would open a TOCTOU gap, so the canonical payload is built once here and the signature
covers its hash.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Artifact:
    """Canonical publish artifact — the payload (what gets sent) and its hash. Immutable."""

    content_id: str
    channel_id: str
    platform: str
    payload: dict  # canonical payload the adapter sends as-is
    payload_hash: str  # canonical sha256 of payload

    def public(self) -> dict:
        """Short form for logs and return values (includes hash, no tokens)."""
        return {
            "content_id": self.content_id,
            "channel_id": self.channel_id,
            "platform": self.platform,
            "payload_hash": self.payload_hash,
        }


def _canonical(payload: dict) -> str:
    """Deterministic serialization (sorted keys, no whitespace) — same payload, same string."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_artifact(content: dict, platform: str) -> Artifact:
    """content.json dict → Artifact.

    The payload holds platform destination, channel, language, mode and body. The body is
    production.draft (the canonical draft). Only the minimum fields needed to publish go in.
    Platform-specific extras (instagram: images) are appended below.
    """
    content_id = content["id"]
    channel_id = content["channel_id"]
    draft = (content.get("production") or {}).get("draft")
    if not draft:
        raise ValueError(f"{content_id}: production.draft is empty; cannot build publish payload")

    # Language and mode follow the first track (single en/native for now)
    lang = "en"
    mode = "native"

    payload = {
        "content_id": content_id,
        "channel_id": channel_id,
        "platform": platform,
        "lang": lang,
        "mode": mode,
        "text": draft,
    }
    # Image formats (card news) include the image set in the canonical payload so the signature covers it.
    # image_gen output arrives as a URL list in production.images. If missing, store an empty list
    # so the adapter rejects it explicitly at publish time (never skip silently).
    if platform == "instagram":
        images = (content.get("production") or {}).get("images") or []
        payload["images"] = list(images)

    # For long-form blog posts the title matters as much as the body, so it goes in the canonical
    # payload under the signature (blocks title-swap TOCTOU). A missing title is rejected here,
    # not at upload time — never upload silently with an empty title.
    if platform == "blogger":
        title = content.get("title")
        if not title:
            raise ValueError(f"{content_id}: title is empty; cannot build blog payload")
        payload["title"] = title

    canonical = _canonical(payload)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Artifact(
        content_id=content_id,
        channel_id=channel_id,
        platform=platform,
        payload=payload,
        payload_hash=payload_hash,
    )
