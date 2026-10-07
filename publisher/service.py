"""service — the only publish entry point. Enforces gates, policy, signature, nonce, send and record in order.

The CLI and server are thin adapters; all gate / signature / nonce enforcement lives here.

Enforcement order (in one place, no bypass):
  1. validate ID + load content.json (shared path helpers — the file is the source of truth)
  2. policy: reject automatic publishing when auto_publish=false (manual channel)
  3. gates: require gate2_ok_at, reject exit states (same rules as mark_published)
     + (after the idempotency check) recompute and compare the Gate2 approval digest — reject if different or missing
  4. idempotency: if published_at already exists, reconcile the nonce and return changed=False
  5. artifact: canonical payload + hash
  6. signature: if require_signature, verify the sidecar (payload_hash binding, nonce, expiry)
  7. nonce: reserve → publishing (state transition right before sending)
  8. transport.send(adapter request): obtain post_id
  9. write-back: platform_id in outputs, published_at / publishing_at, consume nonce
 10. ledger: content.published (module=publish_threads), recorded once via reconcile guard
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.approval import approval_problem
from core.ledger import read_all as read_ledger
from core.ledger import record as record_ledger
from core.content_paths import CNT_RE, _atomic_write, _resolve_content_path

from . import nonce as nonce_mod
from . import adapters
from .artifact import build_artifact
from .policy import PolicyError, load_policy
from .signature import SignatureError, load_key, load_sidecar, verify
from .transport import Transport

# Platforms where only a human makes posts public — publish() (automatic) rejects them.
_DRAFT_ONLY_PLATFORMS = ("blogger",)

_EXIT_KEYS = ("rejected_at", "cancelled_at", "failed_at")


class PublisherError(Exception):
    """Publish contract violation (ID, not found, policy, gate, signature)."""


class PublishBlocked(PublisherError):
    """Blocked by gate, policy or signature enforcement (cannot publish)."""


@dataclass(frozen=True)
class PublishResult:
    content_id: str
    changed: bool
    post_id: str | None = None
    permalink: str | None = None
    published_at: str | None = None
    payload_hash: str | None = None


def publish(
    content_id: str,
    channels_root: Path,
    *,
    transport: Transport,
    ledger_path: Path | None = None,
    secrets_root: Path | None = None,
    nonce_root: Path | None = None,
    sidecar_path: Path | None = None,
    now: str | None = None,
    dry_run: bool = False,
) -> PublishResult:
    """Publish a single content item, applying the enforcement order above in one place.

    With dry_run=True, gates, policy, payload and (signature) are verified but nothing is
    sent or written back — for checking the rail. auto_publish=false channels are rejected
    even in dry_run (the point is to verify policy).
    """
    if not CNT_RE.match(content_id):
        raise PublisherError(f"Invalid content ID format: {content_id!r}")

    cnt_json = _resolve_content_path(content_id, channels_root)
    if cnt_json is None:
        raise PublisherError(f"Content not found: {content_id}")

    content = json.loads(cnt_json.read_text())
    channel_id = content.get("channel_id")
    lifecycle = content.get("lifecycle", {})

    # 2. policy
    try:
        policy = load_policy(channel_id, channels_root)
    except PolicyError as e:
        raise PublisherError(str(e)) from e
    if not policy.allows_auto_publish:
        raise PublishBlocked(
            f"{content_id}: channel {channel_id} has auto_publish=false — automatic publishing not allowed "
            f"(use mark-published for manual publishing)"
        )

    # 3. gate enforcement (same rules as mark_published)
    for exit_key in _EXIT_KEYS:
        if lifecycle.get(exit_key):
            raise PublishBlocked(f"{content_id} is in {exit_key} state — cannot publish")
    if not lifecycle.get("gate2_ok_at"):
        raise PublishBlocked(f"{content_id} has not passed gate2_ok — cannot publish (gate enforced)")

    # Blogs don't use this rail — success from publish() means "live", but for blogs only
    # the draft upload is automatic and a human makes the post public.
    # Draft uploads are handled by publisher.draft_upload.upload_draft.
    if policy.platform in _DRAFT_ONLY_PLATFORMS:
        raise PublishBlocked(
            f"{policy.platform} is not auto-published — use upload_draft for drafts; "
            f"a human makes posts public (no publish-to-live code exists)"
        )

    # 5. artifact (built before the idempotency check so nonce reconciliation can use it)
    artifact = build_artifact(content, policy.platform)
    try:
        adapter = adapters.for_platform(policy.platform)
    except ValueError as e:
        raise PublisherError(str(e)) from e

    if nonce_root is None:
        # default: state/nonce/ next to channels_root
        nonce_root = channels_root.parent / "state" / "nonce"

    # 4. idempotency: already published → reconcile only
    existing_pub = lifecycle.get("published_at")
    if existing_pub:
        existing_post = _existing_post_id(content)
        nonce_mod.reconcile(content_id, nonce_root, published=True, post_id=existing_post)
        _reconcile_ledger(content_id, content, ledger_path)
        return PublishResult(
            content_id=content_id,
            changed=False,
            post_id=existing_post,
            published_at=existing_pub,
            payload_hash=artifact.payload_hash,
        )

    # 5b. approval digest check — body or assets changed after approval are not published (re-approval needed).
    problem = approval_problem(content, cnt_json.parent)
    if problem:
        raise PublishBlocked(f"{content_id}: {problem}")
    # This rail's payload carries the body text only (artifact) — sending only the text of an
    # approved post with images would differ from what was approved. Image posts go out by hand via publish-pack.
    if (content.get("production") or {}).get("assets"):
        raise PublishBlocked(f"{content_id}: post has images — this rail sends text only. Use publish-pack")

    # 6. signature (enforced when auto_publish=true)
    if policy.require_signature:
        if secrets_root is None:
            raise PublishBlocked(f"{content_id}: signature verification requires secrets_root")
        sc_path = sidecar_path or (secrets_root / f"approval_{content_id}.json")
        try:
            sidecar = load_sidecar(sc_path)
            key = load_key(secrets_root)
        except SignatureError as e:
            raise PublishBlocked(str(e)) from e
        # reserve the nonce first and compare it with the one the signature covers
        reserved = nonce_mod.reserve(content_id, artifact.payload_hash, nonce_root)
        try:
            verify(
                sidecar,
                content_id=content_id,
                channel_id=channel_id,
                payload_hash=artifact.payload_hash,
                nonce=reserved.value,
                key=key,
                now=now,
            )
        except SignatureError as e:
            raise PublishBlocked(str(e)) from e
    else:
        reserved = nonce_mod.reserve(content_id, artifact.payload_hash, nonce_root)

    # dry-run: stop after verification, without sending or writing back
    if dry_run:
        return PublishResult(
            content_id=content_id,
            changed=False,
            post_id=None,
            published_at=None,
            payload_hash=artifact.payload_hash,
        )

    # 7. nonce → publishing
    nonce_mod.to_publishing(reserved, nonce_root)

    # 8. send (adapter builds the request, transport injects the token)
    request = adapter.build_request(artifact.payload)
    resp = transport.send(request)
    post_id, permalink = adapter.parse_response(resp)

    # 9. write-back
    ts = now or datetime.now(timezone.utc).isoformat()
    gate2_at = lifecycle.get("gate2_ok_at")
    # ordering invariant: published_at ≥ gate2_ok_at
    published_at = max(ts, gate2_at) if gate2_at else ts
    lifecycle["publishing_at"] = lifecycle.get("publishing_at") or ts
    lifecycle["published_at"] = published_at
    content["lifecycle"] = lifecycle
    outputs = content.setdefault("outputs", [])
    outputs.append({
        "lang": artifact.payload["lang"],
        "mode": artifact.payload["mode"],
        "file": None,
        "platform_id": post_id,
        "permalink": permalink,
    })
    _atomic_write(cnt_json, content)

    # consume nonce (after a successful send)
    nonce_mod.consume(reserved, post_id, nonce_root)

    # 10. ledger
    _reconcile_ledger(content_id, content, ledger_path)

    return PublishResult(
        content_id=content_id,
        changed=True,
        post_id=post_id,
        permalink=permalink,
        published_at=published_at,
        payload_hash=artifact.payload_hash,
    )


def _existing_post_id(content: dict) -> str | None:
    for out in content.get("outputs", []):
        if out.get("platform_id"):
            return out["platform_id"]
    return None


def _reconcile_ledger(content_id: str, content: dict, ledger_path: Path | None) -> None:
    """Record content.published if the ledger lacks it (idempotent repair, module=publish_threads)."""
    if ledger_path is None:
        return
    for e in read_ledger(ledger_path):
        if e.get("event") == "content.published" and e.get("content_id") == content_id:
            return
    record_ledger(
        ledger_path,
        event="content.published",
        content_id=content_id,
        channel_id=content.get("channel_id"),
        module="publish_threads",
    )
