"""nonce — state machine for the publish idempotency key.

A nonce is not "verify then consume" but a **reserved → publishing → consumed** state
plus a publish reconciliation / idempotency key.
- Consume before sending: a failed send can never be retried.
- Missed consume after sending: duplicate publishes during an outage.
→ State is persisted to a file so a "crash mid-send" can be recovered by reconciliation.

State files live outside content.json (state/nonce/<content_id>.json) — schema-independent.
payload_hash is bound so different artifacts cannot reuse the same nonce.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RESERVED = "reserved"
PUBLISHING = "publishing"
CONSUMED = "consumed"


class NonceError(Exception):
    """Nonce state violation (hash mismatch or invalid transition)."""


@dataclass
class Nonce:
    content_id: str
    payload_hash: str
    state: str
    value: str
    post_id: str | None = None


def _path(content_id: str, nonce_root: Path) -> Path:
    return nonce_root / f"{content_id}.json"


def _write(path: Path, nonce: Nonce) -> None:
    nonce_root = path.parent
    nonce_root.mkdir(parents=True, exist_ok=True)
    data = {
        "content_id": nonce.content_id,
        "payload_hash": nonce.payload_hash,
        "state": nonce.state,
        "value": nonce.value,
        "post_id": nonce.post_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def load(content_id: str, nonce_root: Path) -> Nonce | None:
    path = _path(content_id, nonce_root)
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    return Nonce(
        content_id=d["content_id"],
        payload_hash=d["payload_hash"],
        state=d["state"],
        value=d["value"],
        post_id=d.get("post_id"),
    )


def reserve(content_id: str, payload_hash: str, nonce_root: Path) -> Nonce:
    """Put the nonce in reserved state. If one exists, reuse it after checking the hash.

    Same content_id but a different payload_hash (artifact changed) → NonceError —
    prevents pushing different content under the nonce of an earlier publish attempt.
    """
    existing = load(content_id, nonce_root)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise NonceError(
                f"{content_id}: nonce is reserved for a different artifact "
                f"(existing {existing.payload_hash[:8]} != requested {payload_hash[:8]})"
            )
        return existing
    nonce = Nonce(
        content_id=content_id,
        payload_hash=payload_hash,
        state=RESERVED,
        value=uuid.uuid4().hex,
    )
    _write(_path(content_id, nonce_root), nonce)
    return nonce


def to_publishing(nonce: Nonce, nonce_root: Path) -> Nonce:
    """reserved → publishing. Call right before sending. Idempotent (keeps publishing/consumed as is)."""
    if nonce.state == CONSUMED:
        return nonce
    nonce.state = PUBLISHING
    _write(_path(nonce.content_id, nonce_root), nonce)
    return nonce


def consume(nonce: Nonce, post_id: str, nonce_root: Path) -> Nonce:
    """publishing → consumed. Call after a successful send. Binds post_id. Idempotent."""
    if nonce.state == CONSUMED:
        return nonce
    nonce.state = CONSUMED
    nonce.post_id = post_id
    _write(_path(nonce.content_id, nonce_root), nonce)
    return nonce


def reconcile(content_id: str, nonce_root: Path, published: bool, post_id: str | None) -> None:
    """Recovery: if left in publishing but already published, correct it to consumed."""
    nonce = load(content_id, nonce_root)
    if nonce is None:
        return
    if nonce.state == PUBLISHING and published:
        nonce.state = CONSUMED
        if post_id and not nonce.post_id:
            nonce.post_id = post_id
        _write(_path(content_id, nonce_root), nonce)
