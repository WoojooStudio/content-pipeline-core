"""publisher.adapters — per-platform request building and response parsing (no credentials or nonces).

Adapters follow a structural contract exposing (build_request, parse_response, PLATFORM).
service picks an adapter via for_platform(policy.platform) — no hard-coded branching.
"""
from types import ModuleType

from . import blogger, instagram, threads

__all__ = ["threads", "instagram", "blogger", "for_platform"]

_BY_PLATFORM: dict[str, ModuleType] = {
    threads.PLATFORM: threads,
    instagram.PLATFORM: instagram,
    blogger.PLATFORM: blogger,
}


def for_platform(platform: str) -> ModuleType:
    """Platform name → adapter module. Unsupported platforms raise ValueError (wrapping KeyError)."""
    try:
        return _BY_PLATFORM[platform]
    except KeyError:
        raise ValueError(
            f"Unsupported publishing platform: {platform!r} (supported: {sorted(_BY_PLATFORM)})"
        ) from None
