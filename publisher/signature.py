"""signature — verifies the sidecar approval token right before an auto_publish=true publish.

content.json's by/provenance fields can be forged by the orchestrator, so they are not trusted.
Instead, a sidecar token signed by a human-only helper is verified at publish time — the token
binds (content_id + payload_hash + channel + approval time + nonce + approver identity), and the
signing key lives where the LLM's OS user/process cannot reach it (secrets/).

**The signing key is not in this code** (a human provides it). This module only verifies. The
signing helper is run separately by a human (outside this repo or from secrets/); here we only
perform HMAC-SHA256 verification.

Residual risk: if the LLM can reach the key from the same process or OS user, this is still soft.
That limit is an explicitly accepted contract — this module builds a "human approves, machine
executes" rail.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class SignatureError(Exception):
    """Signature verification failed (missing, mismatch, hash binding violation, or no key)."""


@dataclass(frozen=True)
class Sidecar:
    """Human-signed approval token. Lives outside content.json (secrets/ or state/)."""

    content_id: str
    channel_id: str
    payload_hash: str
    nonce: str
    approved_at: str
    approver: str
    signature: str  # hex HMAC-SHA256 over the canonical binding


def _binding(content_id: str, channel_id: str, payload_hash: str,
             nonce: str, approved_at: str, approver: str) -> bytes:
    """Canonical binding covered by the signature — fixed field order."""
    canonical = json.dumps(
        {
            "content_id": content_id,
            "channel_id": channel_id,
            "payload_hash": payload_hash,
            "nonce": nonce,
            "approved_at": approved_at,
            "approver": approver,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical.encode("utf-8")


def load_sidecar(path: Path) -> Sidecar:
    """Read the sidecar token file. Raises SignatureError if missing."""
    if not path.exists():
        raise SignatureError(f"Approval sidecar token not found: {path} (human signature required)")
    d = json.loads(path.read_text(encoding="utf-8"))
    try:
        return Sidecar(
            content_id=d["content_id"],
            channel_id=d["channel_id"],
            payload_hash=d["payload_hash"],
            nonce=d["nonce"],
            approved_at=d["approved_at"],
            approver=d["approver"],
            signature=d["signature"],
        )
    except KeyError as e:
        raise SignatureError(f"Sidecar token is missing a field: {e}") from e


def load_key(secrets_root: Path, key_ref: str = "publish_signing_key") -> bytes:
    """Read the verification key from secrets/<key_ref>. Raises SignatureError if missing.

    The key is provided by a human — nothing is hard-coded.
    """
    path = secrets_root / key_ref
    if not path.exists():
        raise SignatureError(
            f"Signature verification key not found: {path} (create secrets/{key_ref})"
        )
    key = path.read_bytes().strip()
    if not key:
        raise SignatureError(f"Signature verification key is empty: {path}")
    return key


def verify(
    sidecar: Sidecar,
    *,
    content_id: str,
    channel_id: str,
    payload_hash: str,
    nonce: str,
    key: bytes,
    now: str | None = None,
    max_age_sec: int = 3600,
) -> None:
    """Verify the sidecar signature. Raises SignatureError on failure.

    - bound fields match the publish target (content_id, channel, payload_hash, nonce)
    - the HMAC signature is valid (constant-time compare)
    - the approval is not too old (limits the replay window)
    """
    if sidecar.content_id != content_id:
        raise SignatureError(f"Sidecar content_id mismatch: {sidecar.content_id} != {content_id}")
    if sidecar.channel_id != channel_id:
        raise SignatureError(f"Sidecar channel_id mismatch: {sidecar.channel_id} != {channel_id}")
    if sidecar.payload_hash != payload_hash:
        raise SignatureError(
            f"Sidecar payload_hash mismatch — artifact changed after approval (TOCTOU): "
            f"{sidecar.payload_hash[:8]} != {payload_hash[:8]}"
        )
    if sidecar.nonce != nonce:
        raise SignatureError(f"Sidecar nonce mismatch: {sidecar.nonce} != {nonce}")

    expected = hmac.new(
        key,
        _binding(content_id, channel_id, payload_hash, nonce,
                 sidecar.approved_at, sidecar.approver),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sidecar.signature):
        raise SignatureError("Sidecar signature is invalid (forged or key mismatch)")

    now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    try:
        approved_dt = datetime.fromisoformat(sidecar.approved_at)
    except ValueError as e:
        raise SignatureError(f"Sidecar approved_at is malformed: {sidecar.approved_at}") from e
    age = (now_dt - approved_dt).total_seconds()
    if age > max_age_sec:
        raise SignatureError(
            f"Sidecar approval expired ({age:.0f}s > {max_age_sec}s replay window)"
        )


def sign(
    *,
    content_id: str,
    channel_id: str,
    payload_hash: str,
    nonce: str,
    approver: str,
    key: bytes,
    approved_at: str | None = None,
) -> Sidecar:
    """Create a signature for tests and the human helper. In production the key never leaves secrets/."""
    approved_at = approved_at or datetime.now(timezone.utc).isoformat()
    signature = hmac.new(
        key,
        _binding(content_id, channel_id, payload_hash, nonce, approved_at, approver),
        hashlib.sha256,
    ).hexdigest()
    return Sidecar(
        content_id=content_id,
        channel_id=channel_id,
        payload_hash=payload_hash,
        nonce=nonce,
        approved_at=approved_at,
        approver=approver,
        signature=signature,
    )
