"""adapters/threads — Threads API request building and response parsing only.

**Adapters own no credentials or nonces.** It turns the canonical payload into a Threads
API request and extracts post_id and permalink from the response. The transport injects
the token at the last moment.

Threads Graph API two-step publishing (graph.threads.net):
  1) POST /{account_id}/threads         (create media container) → creation_id
  2) POST /{account_id}/threads_publish (publish container)      → post_id
The adapter only describes the shape of these two requests. Real HTTP and tokens belong
to the transport.
"""
from __future__ import annotations

PLATFORM = "threads"


def build_request(payload: dict) -> dict:
    """Canonical payload → Threads publish request (no token or account ID).

    The transport fills in account_id and token to build the real URL and body. This
    only holds a platform-neutral description of what to post.
    """
    text = payload.get("text")
    if not text:
        raise ValueError("Failed to build threads request: payload.text is empty")
    return {
        "platform": PLATFORM,
        "media_type": "TEXT",
        "text": text,
    }


def parse_response(resp: dict) -> tuple[str, str]:
    """Threads API response → (post_id, permalink).

    A publish response is at least {"id": "<post_id>"}. Use permalink if present, otherwise build one.
    """
    post_id = resp.get("id")
    if not post_id:
        raise ValueError(f"threads response has no post id: {resp!r}")
    permalink = resp.get("permalink") or f"https://www.threads.net/t/{post_id}"
    return str(post_id), permalink
