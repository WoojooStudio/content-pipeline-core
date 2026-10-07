"""Gate checks — verify gate1/gate2 in content.json and SystemExit if not passed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_GATE_LC_KEY = {
    "g0": ("gate0_pending_at", "gate0_ok_at"),
    "g1": ("gate1_pending_at", "gate1_ok_at"),
    "g2": ("gate2_pending_at", "gate2_ok_at"),
}


class GateError(Exception):
    pass


def require_gate1(content_path: Path) -> None:
    """Check gate1_ok before calling cost-incurring modules. Reject and exit if not passed."""
    content = _load(content_path)
    if not _gate_ok(content, "g1"):
        _reject(content_path, "gate1", "gate1_ok_at is missing. Re-run after Gate1 approval.")


def require_gate2(content_path: Path) -> None:
    """Check gate2_ok before calling publishing modules. Reject and exit if not passed."""
    content = _load(content_path)
    if not _gate_ok(content, "g2"):
        _reject(content_path, "gate2", "gate2_ok_at is missing. Re-run after Gate2 approval.")


def submit_gate(content_path: Path, gate: str, at: str) -> None:
    """Record the gate0/gate1/gate2 review-requested (pending) state in lifecycle."""
    if gate not in _GATE_LC_KEY:
        raise GateError(f"gate must be 'g0', 'g1', or 'g2', got: {gate!r}")
    content = _load(content_path)
    pending_key, _ = _GATE_LC_KEY[gate]
    content["lifecycle"][pending_key] = at
    _save(content_path, content)


def record_gate(content_path: Path, gate: str, checks: dict[str, bool], by: str, at: str,
                provenance: dict | None = None) -> None:
    """Record a gate0/gate1/gate2 approval result in content.json.

    Every value in checks must be True to approve. Any False raises GateError.
    """
    if gate not in _GATE_LC_KEY:
        raise GateError(f"gate must be 'g0', 'g1', or 'g2', got: {gate!r}")

    failed = [k for k, v in checks.items() if not v]
    if failed:
        raise GateError(f"Gate checks failed: {failed}. All checks must be True to approve.")

    content = _load(content_path)
    content["gates"][gate]["checks"] = checks
    content["gates"][gate]["by"] = by
    content["gates"][gate]["at"] = at
    if provenance is not None:
        content["gates"][gate]["provenance"] = provenance

    _, ok_key = _GATE_LC_KEY[gate]
    content["lifecycle"][ok_key] = at
    _save(content_path, content)


def _gate_ok(content: dict, gate: str) -> bool:
    _, ok_key = _GATE_LC_KEY[gate]
    return bool(content.get("lifecycle", {}).get(ok_key))


def _load(path: Path) -> dict:
    if not path.exists():
        raise GateError(f"content.json not found: {path}")
    return json.loads(path.read_text())


def _save(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2))


def _reject(content_path: Path, gate_name: str, message: str) -> None:
    print(f"[gate] REJECTED — {gate_name}: {message}", file=sys.stderr)
    print(f"[gate] content: {content_path}", file=sys.stderr)
    sys.exit(1)
