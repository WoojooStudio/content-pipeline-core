"""Gate2 approval digest — the single computation that pins "what the human approved" to one value.

The approval covers more than the draft text. For visual formats (carousel, reel) the image/video
files and final caption are what actually ships, so if any file or the order changes after
approval, that approval must be void.

What the digest covers (canonical manifest → sorted JSON → sha256):
  - content id · channel_id · format
  - production.draft (script/body) · production.caption (final caption)
  - production.assets[] — sorted by order; each item's file, role, order and
    **a sha256 computed by re-reading the file at approval/verification time** (recorded sha256 is not trusted)

This is separate from draft_sha (comment revision, sha256(draft)[:12]). draft_sha identifies which
draft a comment was made on; this digest is the object of Gate2 approval.

Compatibility: content approved before v5 has no digest. It is not backfilled here —
an approval without a digest counts as "not currently approved" and a human re-approves via approve-gate2.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# Structure version of the canonical digest manifest — bump when the computation changes (existing approvals then need re-approval).
DIGEST_VERSION = 1

# Formats that must go through production (file outputs) before Gate2 — values set by production-start.
VISUAL_FORMATS = ("carousel", "reel")

# Non-text formats — if content.format or the channel profile.format is one of these, it cannot reach Gate2
# without production. Production can only settle on VISUAL_FORMATS, so card/image/video mean
# "needs production but not settled yet" (production-start settles it to carousel|reel).
PRODUCTION_REQUIRED_FORMATS = ("carousel", "reel", "card", "image", "video")

# Content on these platforms always requires completed production, whatever the format says (no text posts).
PRODUCTION_PLATFORMS = ("instagram", "tiktok")

ASSET_ROLES = ("slide", "video", "cover", "audio", "caption")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_VIDEO_EXTS = (".mp4", ".mov")
_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac")
_CAPTION_EXTS = (".srt", ".vtt", ".txt")

# Allowed extensions per role — keeps unrelated files (content.json etc.) from being registered as assets.
ROLE_EXTS: dict[str, tuple[str, ...]] = {
    "slide": _IMAGE_EXTS,
    "cover": _IMAGE_EXTS,
    "video": _VIDEO_EXTS,
    "audio": _AUDIO_EXTS,
    "caption": _CAPTION_EXTS,
}

CAROUSEL_MIN_SLIDES = 2
CAROUSEL_MAX_SLIDES = 10

_REOPEN_KEYS = ("producing_at", "produced_at", "gate2_pending_at")


class ApprovalError(Exception):
    """Asset path/existence/format-completeness violation — digest cannot be computed or the approval target is incomplete."""


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_asset(content_dir: Path, rel: str) -> Path:
    """Asset relative path → real path. Rejects anything outside the content unit directory (including via symlinks).

    Existence is checked by the caller (so the missing-file message fits the context).
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ApprovalError(f"Asset file is empty: {rel!r}")
    if any(ord(ch) < 0x20 or ch in '"\x7f' for ch in rel):
        raise ApprovalError(f"Asset file contains control characters or quotes: {rel!r}")
    if "\\" in rel or rel.startswith("/") or Path(rel).is_absolute():
        raise ApprovalError(f"Asset file must be a path relative to the content unit: {rel!r}")
    if any(part in ("..", ".", "") for part in rel.split("/")):
        raise ApprovalError(f"Asset file contains '..', '.' or empty segments: {rel!r}")
    base = content_dir.resolve()
    target = (content_dir / rel).resolve()
    if target == base or not target.is_relative_to(base):
        raise ApprovalError(f"Asset points outside the content unit: {rel!r}")
    return target


def check_asset_file(path: Path, rel: str) -> None:
    """An asset must be an existing regular file and not a hard link.

    A hard link can share an inode with a file outside the unit (another unit, a temp folder),
    letting the file change from outside. Generation tools never create hard links, so reject them.
    """
    if not path.is_file():
        raise ApprovalError(f"Asset file not found: {rel}")
    if path.stat().st_nlink > 1:
        raise ApprovalError(f"Asset file is a hard link ({path.stat().st_nlink} links): {rel}")


def requires_production(content: dict, channel_format: str | None,
                        channel_platform: str | None) -> bool:
    """Whether this content must complete production before Gate2.

    Only uses values set by a human or the CLI — content.format (set by production-start),
    and the channel profile's format/platform. experiment.format is free text written by an LLM,
    so it is not used here (it must not be a way to skip the production requirement).
    """
    if content.get("format") in PRODUCTION_REQUIRED_FORMATS:
        return True
    if isinstance(channel_format, str) and channel_format.strip().lower() in PRODUCTION_REQUIRED_FORMATS:
        return True
    return channel_platform in PRODUCTION_PLATFORMS


def validate_completeness(fmt: str | None, assets: list[dict]) -> None:
    """Per-format asset completeness. carousel: 2–10 image slides, no video. reel: exactly 1 video, no slides."""
    if fmt not in VISUAL_FORMATS:
        return
    roles = [a.get("role") for a in assets]
    n_slide = roles.count("slide")
    n_video = roles.count("video")
    if fmt == "carousel":
        if not (CAROUSEL_MIN_SLIDES <= n_slide <= CAROUSEL_MAX_SLIDES):
            raise ApprovalError(
                f"carousel needs {CAROUSEL_MIN_SLIDES}–{CAROUSEL_MAX_SLIDES} slide images "
                f"(got {n_slide})"
            )
        if n_video:
            raise ApprovalError("carousel has a video asset")
    elif fmt == "reel":
        if n_video != 1:
            raise ApprovalError(f"reel needs exactly 1 video (got {n_video})")
        if n_slide:
            raise ApprovalError("reel has slide assets")
        if roles.count("cover") > 1:
            raise ApprovalError("reel allows at most 1 cover")


def _ordered(assets: list[dict]) -> list[dict]:
    return sorted(assets, key=lambda a: a.get("order", 0))


def current_assets(content: dict, content_dir: Path) -> list[dict]:
    """Return production.assets in order, with sha256 computed by **re-reading** each file.

    Missing or out-of-bounds files raise ApprovalError. Recorded sha256 values are not used.
    """
    assets = (content.get("production") or {}).get("assets") or []
    out: list[dict] = []
    for a in _ordered(assets):
        path = resolve_asset(content_dir, a.get("file"))
        check_asset_file(path, a.get("file"))
        out.append({
            "file": a.get("file"),
            "role": a.get("role"),
            "order": a.get("order"),
            "sha256": file_sha256(path),
        })
    return out


def approval_manifest(content: dict, content_dir: Path) -> dict:
    """Canonical digest input. The same approval target always yields the same dict."""
    production = content.get("production") or {}
    return {
        "v": DIGEST_VERSION,
        "id": content.get("id"),
        "channel_id": content.get("channel_id"),
        "format": content.get("format"),
        "draft": production.get("draft") or "",
        "caption": production.get("caption"),
        "assets": current_assets(content, content_dir),
    }


def approval_digest(content: dict, content_dir: Path) -> str:
    """Approval digest (64-char sha256 hex). Computed by re-reading asset files."""
    canon = json.dumps(
        approval_manifest(content, content_dir),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def registered_mismatches(content: dict, content_dir: Path) -> list[str]:
    """List of asset files whose recorded sha256 in production.assets differs from the file on disk.

    Files changed after production-complete without re-registering the manifest —
    caught before approval so nothing is approved while "registered ≠ on disk".
    """
    recorded = {a.get("file"): a.get("sha256") for a in
                (content.get("production") or {}).get("assets") or []}
    return [a["file"] for a in current_assets(content, content_dir)
            if recorded.get(a["file"]) != a["sha256"]]


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def stored_digest(content: dict) -> str | None:
    g2 = (content.get("gates") or {}).get("g2") or {}
    return (g2.get("provenance") or {}).get("approval_digest")


def reopened_after_approval(lifecycle: dict) -> str | None:
    """The lifecycle key that reopened production after approval (later than gate2_ok_at), if any."""
    ok = _ts(lifecycle.get("gate2_ok_at"))
    if ok is None:
        return None
    for key in _REOPEN_KEYS:
        t = _ts(lifecycle.get(key))
        if t is not None and t > ok:
            return key
    return None


def approval_problem(content: dict, content_dir: Path) -> str | None:
    """Whether the current Gate2 approval is valid. None if valid, otherwise a human-readable reason.

    Valid when: gate2_ok_at is set + a stored digest exists + production was not reopened after approval +
    the digest recomputed now == the stored digest.
    """
    lifecycle = content.get("lifecycle") or {}
    if not lifecycle.get("gate2_ok_at"):
        return "before gate2_ok"
    stored = stored_digest(content)
    if not stored:
        return ("Approval has no digest (pre-v6 record) — cannot verify what was approved. "
                "Re-approve with approve-gate2")
    reopened = reopened_after_approval(lifecycle)
    if reopened:
        return f"Production was reopened after approval ({reopened}) — re-approve with approve-gate2"
    try:
        current = approval_digest(content, content_dir)
    except ApprovalError as e:
        return f"Cannot recompute the approval target: {e}"
    if current != stored:
        return ("Content changed after approval (draft, caption, asset file or order) — "
                f"approved {stored[:12]} ≠ current {current[:12]}. Re-approve with approve-gate2")
    return None
