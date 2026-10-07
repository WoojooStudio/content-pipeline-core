"""Production stage records — production-start / production-complete (content v6).

The only writer that binds visual outputs (images, video) to a content unit between Gate1 and Gate2.
Generation itself happens in external tools; this module **only records**:

- production_start: check Gate1 passed → settle format (carousel|reel) → producing_at
- production_complete: validate the asset manifest (path isolation, existence, extension, order, format completeness) →
  read files for sha256 → write production.assets and caption → produced_at + gate2_pending_at

Contract:
- resolve IDs by scanning the source-of-truth files (content.json); atomic writes (tmp + os.replace)
- reject published or exited content, and anything before Gate1
- idempotent: re-running with the same input gives changed=False and no duplicate ledger lines
- keep timestamp order: gate1_ok_at ≤ producing_at ≤ produced_at = gate2_pending_at
  (status derives from the latest timestamp, so an inversion shows the wrong state)
- re-production after approval (re-running production_complete) is allowed — gate2_pending_at becomes
  later than gate2_ok_at and the digest changes, so the old approval is void (re-approval needed)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .approval import (
    ASSET_ROLES,
    ROLE_EXTS,
    VISUAL_FORMATS,
    ApprovalError,
    approval_digest,
    check_asset_file,
    requires_production,
    file_sha256,
    resolve_asset,
    validate_completeness,
)
from .ledger import read_all as read_ledger
from .ledger import record as record_ledger

CNT_RE = re.compile(r"^CNT-[0-9]{3}-[0-9]{4}$")
CONTENT_SCHEMA_VERSION = 6

_EXIT_KEYS = ("rejected_at", "cancelled_at", "failed_at")
_MANIFEST_KEYS = {"caption", "assets"}
_ASSET_KEYS = {"file", "role", "order", "sha256"}
_CAPTION_MAX = 2200  # Instagram caption limit
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ProductionError(Exception):
    """Production record contract violation (ID, transition, manifest, assets)."""


@dataclass(frozen=True)
class ProductionResult:
    content_id: str
    at: str
    changed: bool
    format: str
    assets: tuple = ()
    approval_digest: str | None = None


def production_start(
    content_id: str,
    channels_root: Path,
    fmt: str,
    *,
    ledger_path: Path | None = None,
    at: str | None = None,
) -> ProductionResult:
    """Record production start. Requires Gate1. Idempotent if already started (changed=False)."""
    cnt_json, content = _load_checked(content_id, channels_root)
    lifecycle = content["lifecycle"]

    if fmt not in VISUAL_FORMATS:
        raise ProductionError(f"Production format must be one of {VISUAL_FORMATS}: {fmt!r}")
    existing_fmt = content.get("format")
    if existing_fmt is not None and existing_fmt != fmt:
        raise ProductionError(
            f"{content_id} format is already {existing_fmt!r} — cannot change to {fmt!r}"
        )

    if lifecycle.get("producing_at") and existing_fmt == fmt:
        _ledger_once(ledger_path, "production.started", content,
                     {"format": fmt, "at": lifecycle["producing_at"]}, match_keys=("at",))
        return ProductionResult(content_id, lifecycle["producing_at"], False, fmt)

    ts = _later(at or _now(), lifecycle.get("gate1_ok_at"))
    content["format"] = fmt
    content["schema_version"] = CONTENT_SCHEMA_VERSION
    lifecycle["producing_at"] = ts
    _atomic_write(cnt_json, content)
    _ledger_once(ledger_path, "production.started", content,
                 {"format": fmt, "at": ts}, match_keys=("at",))
    return ProductionResult(content_id, ts, True, fmt)


def production_complete(
    content_id: str,
    channels_root: Path,
    manifest: dict,
    *,
    ledger_path: Path | None = None,
    at: str | None = None,
) -> ProductionResult:
    """Validate and register the asset manifest and record production complete (+ Gate2 submission).

    manifest: {"caption": str, "assets": [{"file", "role", "order", "sha256"?}]}
      file is relative to the content unit directory. sha256 is computed here by reading the file
      (if present in the manifest it is only compared — mismatch is rejected).
    """
    cnt_json, content = _load_checked(content_id, channels_root)
    lifecycle = content["lifecycle"]
    content_dir = cnt_json.parent

    if not lifecycle.get("producing_at"):
        raise ProductionError(f"{content_id} production not started — run production-start first")
    fmt = content.get("format")
    if fmt not in VISUAL_FORMATS:
        raise ProductionError(f"{content_id} format ({fmt!r}) is not a production format")

    caption, assets = _validate_manifest(manifest, content_dir)
    try:
        validate_completeness(fmt, assets)
    except ApprovalError as e:
        raise ProductionError(str(e)) from e

    production = content.setdefault("production", {})
    same = (
        production.get("assets") == assets
        and production.get("caption") == caption
        and bool(lifecycle.get("produced_at"))
        and _parse(lifecycle["produced_at"]) >= _parse(lifecycle["producing_at"])
    )
    if same:
        digest = approval_digest(content, content_dir)
        _ledger_once(ledger_path, "production.completed", content,
                     {"format": fmt, "n_assets": len(assets), "digest": digest,
                      "at": lifecycle["produced_at"]},
                     match_keys=("digest", "at"))
        return ProductionResult(content_id, lifecycle["produced_at"], False, fmt,
                                tuple(a["file"] for a in assets), digest)

    ts = _later(at or _now(), lifecycle["producing_at"])
    production["assets"] = assets
    production["caption"] = caption
    production.setdefault("brief", None)
    production.setdefault("human_minutes", 0)
    lifecycle["produced_at"] = ts
    lifecycle["gate2_pending_at"] = ts
    content["schema_version"] = CONTENT_SCHEMA_VERSION
    digest = approval_digest(content, content_dir)
    _atomic_write(cnt_json, content)
    _ledger_once(ledger_path, "production.completed", content,
                 {"format": fmt, "n_assets": len(assets), "digest": digest, "at": ts},
                 match_keys=("digest", "at"))
    return ProductionResult(content_id, ts, True, fmt,
                            tuple(a["file"] for a in assets), digest)


TEXT_IMAGE_MAX = 4  # image limit for text posts — the text leads, images support


def attach_images(
    content_id: str,
    channels_root: Path,
    files: list[str],
    *,
    ledger_path: Path | None = None,
    at: str | None = None,
) -> ProductionResult:
    """Attach images to a text-format post (threads etc.) — without the production stage (production-start/complete).

    files: paths relative to the content unit directory, given order 1..n in sequence, role "slide" (ordered images).
    Attaching reopens Gate2 (gate2_pending_at) — the approval digest covers asset file bytes, so
    the old approval is void and a human must review the images and re-approve. Same file list is idempotent.
    An empty list removes attached images (also needs re-approval).
    Visual formats (content needing production) are rejected — those are owned by the production-complete manifest.
    """
    cnt_json, content = _load_checked(content_id, channels_root)
    lifecycle = content["lifecycle"]
    content_dir = cnt_json.parent
    profile = _profile(content_dir)
    if requires_production(content, profile.get("format"), profile.get("platform")):
        raise ProductionError(f"{content_id} is a visual format — register images via the production-complete manifest")
    if not isinstance(files, list) or len(files) > TEXT_IMAGE_MAX:
        raise ProductionError(f"Images must be 0–{TEXT_IMAGE_MAX}")
    manifest = {"caption": "-", "assets": [{"file": f, "role": "slide", "order": i}
                                          for i, f in enumerate(files, start=1)]}
    if files:
        _, assets = _validate_manifest(manifest, content_dir)
    else:
        assets = []

    production = content.setdefault("production", {})
    if (production.get("assets") or []) == assets:
        return ProductionResult(content_id, lifecycle.get("gate2_pending_at") or "", False,
                                content.get("format") or "", tuple(a["file"] for a in assets),
                                approval_digest(content, content_dir))
    ts = _reopen_ts(at, lifecycle)
    if assets:
        production["assets"] = assets
    else:
        production.pop("assets", None)
    lifecycle["gate2_pending_at"] = ts
    content["schema_version"] = CONTENT_SCHEMA_VERSION
    digest = approval_digest(content, content_dir)
    _atomic_write(cnt_json, content)
    _ledger_once(ledger_path, "production.images_attached", content,
                 {"files": [a["file"] for a in assets], "digest": digest, "at": ts},
                 match_keys=("digest", "at"))
    return ProductionResult(content_id, ts, True, content.get("format") or "",
                            tuple(a["file"] for a in assets), digest)


def revise_draft(
    content_id: str,
    channels_root: Path,
    body: str,
    *,
    ledger_path: Path | None = None,
    at: str | None = None,
) -> ProductionResult:
    """Revise the body after Gate1 (human comments, review bounce, over length, etc.). Reopens Gate2.

    The body is covered by the approval digest, so editing voids the old approval — a human must re-approve
    before publish packs or publish marking. The old and new body sha go to the ledger (content.revised)
    to track what changed. Same body is idempotent. Published, exited or pre-Gate1 content is rejected (_load_checked).
    """
    if not isinstance(body, str) or not body.strip():
        raise ProductionError("New body is empty")
    if "\r" in body:
        raise ProductionError("Body contains CR (\\r) line breaks — use LF")
    cnt_json, content = _load_checked(content_id, channels_root)
    lifecycle = content["lifecycle"]
    production = content.setdefault("production", {})
    old = production.get("draft") or ""
    if old == body:
        return ProductionResult(content_id, lifecycle.get("gate2_pending_at") or "", False,
                                content.get("format") or "")
    ts = _reopen_ts(at, lifecycle)
    production["draft"] = body
    lifecycle["gate2_pending_at"] = ts
    content["schema_version"] = CONTENT_SCHEMA_VERSION
    digest = approval_digest(content, cnt_json.parent)
    _atomic_write(cnt_json, content)
    import hashlib

    _ledger_once(ledger_path, "content.revised", content,
                 {"from_sha": hashlib.sha256(old.encode()).hexdigest()[:12],
                  "to_sha": hashlib.sha256(body.encode()).hexdigest()[:12],
                  "digest": digest, "at": ts},
                 match_keys=("to_sha", "at"))
    return ProductionResult(content_id, ts, True, content.get("format") or "",
                            tuple(a["file"] for a in production.get("assets") or []), digest)


def _profile(content_dir: Path) -> dict:
    try:
        return json.loads((content_dir.parent.parent / "profile.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ProductionError(f"Cannot read manifest: {path} ({e})") from e
    if not isinstance(data, dict):
        raise ProductionError("Manifest must be a JSON object")
    return data


# ---------------------------------------------------------------------------


def _validate_manifest(manifest: dict, content_dir: Path) -> tuple[str, list[dict]]:
    if not isinstance(manifest, dict):
        raise ProductionError("Manifest must be a JSON object")
    unknown = set(manifest) - _MANIFEST_KEYS
    if unknown:
        raise ProductionError(f"Unknown keys in manifest: {sorted(unknown)}")

    caption = manifest.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        raise ProductionError("caption (final post caption) is empty")
    if len(caption) > _CAPTION_MAX:
        raise ProductionError(f"caption exceeds {_CAPTION_MAX} characters ({len(caption)})")

    raw = manifest.get("assets")
    if not isinstance(raw, list) or not raw:
        raise ProductionError("assets is empty")

    out: list[dict] = []
    # Detect duplicates by inode, not string — on case-insensitive filesystems (macOS default)
    # assets/01.png and assets/01.PNG are the same file.
    seen_files: set[tuple[int, int]] = set()
    seen_orders: set[int] = set()
    for i, a in enumerate(raw, start=1):
        if not isinstance(a, dict):
            raise ProductionError(f"assets[{i}] must be an object")
        unknown = set(a) - _ASSET_KEYS
        if unknown:
            raise ProductionError(f"Unknown keys in assets[{i}]: {sorted(unknown)}")
        rel = a.get("file")
        role = a.get("role")
        order = a.get("order")
        if role not in ASSET_ROLES:
            raise ProductionError(f"assets[{i}].role must be one of {ASSET_ROLES}: {role!r}")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ProductionError(f"assets[{i}].order must be an integer ≥ 1: {order!r}")
        if order in seen_orders:
            raise ProductionError(f"Duplicate assets order: {order}")
        try:
            path = resolve_asset(content_dir, rel)
        except ApprovalError as e:
            raise ProductionError(str(e)) from e
        try:
            check_asset_file(path, rel)
        except ApprovalError as e:
            raise ProductionError(str(e)) from e
        st = path.stat()
        identity = (st.st_dev, st.st_ino)
        if identity in seen_files:
            raise ProductionError(f"Same file registered twice: {rel}")
        if path.suffix.lower() not in ROLE_EXTS[role]:
            raise ProductionError(
                f"{rel}: extension not allowed for role {role!r} (allowed {ROLE_EXTS[role]})"
            )
        sha = file_sha256(path)
        declared = a.get("sha256")
        if declared is not None:
            if not isinstance(declared, str) or not _SHA_RE.match(declared):
                raise ProductionError(f"assets[{i}].sha256 has an invalid format")
            if declared != sha:
                raise ProductionError(f"{rel}: manifest sha256 does not match the file")
        seen_files.add(identity)
        seen_orders.add(order)
        out.append({"file": rel, "role": role, "order": order, "sha256": sha})

    out.sort(key=lambda x: x["order"])
    return caption, out


def _load_checked(content_id: str, channels_root: Path) -> tuple[Path, dict]:
    """Resolve the ID + common transition checks (reject published/exited, require Gate1)."""
    if not CNT_RE.match(content_id):
        raise ProductionError(f"Invalid content ID format: {content_id!r}")
    cnt_json = _resolve(content_id, channels_root)
    if cnt_json is None:
        raise ProductionError(f"Content not found: {content_id}")
    content = json.loads(cnt_json.read_text(encoding="utf-8"))
    lifecycle = content.setdefault("lifecycle", {})
    if lifecycle.get("published_at"):
        raise ProductionError(f"{content_id} is already published — cannot reopen production")
    for key in _EXIT_KEYS:
        if lifecycle.get(key):
            raise ProductionError(f"{content_id} is in state {key} — cannot record production")
    if not lifecycle.get("gate1_ok_at"):
        raise ProductionError(f"{content_id} is before gate1_ok — cannot produce (gate enforced)")
    return cnt_json, content


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
                raise ProductionError(
                    f"content.json id ({data.get('id')}) does not match the directory/request ({content_id})"
                )
            return candidate
    return None


def _ledger_once(ledger_path: Path | None, event: str, content: dict, meta: dict,
                 match_keys: tuple[str, ...]) -> None:
    """Record only if the same (event, content_id, meta[match_keys…]) is absent — prevents duplicates on retry.

    Completion events are matched on (digest, at). With digest alone, in "complete with A → redo with B → back to A"
    the third completion shares the first one's digest and is lost (even though approval was voided in between).
    """
    if ledger_path is None:
        return
    cid = content.get("id")
    for e in read_ledger(ledger_path):
        m = e.get("meta") or {}
        if (e.get("event") == event and e.get("content_id") == cid
                and all(m.get(k) == meta.get(k) for k in match_keys)):
            return
    record_ledger(ledger_path, event=event, content_id=cid,
                  channel_id=content.get("channel_id"), module="production", meta=meta)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reopen_ts(at: str | None, lifecycle: dict) -> str:
    """Time to reopen Gate2 — after gate1_ok and **strictly** later than gate2_ok.

    With equal times, status derivation and approval_problem's "reopened after approval" check (strict >)
    lean towards approval, so a reopened Gate2 looks approved and an A→B→A revert revives the approval.
    """
    ts = _later(at or _now(), lifecycle.get("gate1_ok_at"))
    ok = lifecycle.get("gate2_ok_at")
    if ok and _parse(ts) <= _parse(ok):
        from datetime import timedelta
        ts = (_parse(ok) + timedelta(microseconds=1)).isoformat()
    return ts


def _later(a: str, b: str | None) -> str:
    """The later of two ISO times (a if b is missing). Compared as instants even if timezones differ."""
    if not b:
        return a
    return a if _parse(a) >= _parse(b) else b


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
