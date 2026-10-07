"""adapters/blogger — Google Blogger API v3 request building and response parsing only.

Same contract as the other adapters: **it owns no credentials or nonces.** It turns the
canonical payload into a Blogger request and extracts post_id and url from the response;
token injection is the transport's job.

**Draft upload only — there is no code that makes a post public.** Specifically:

- `posts.insert` is always called with `isDraft=true`. Callers cannot flip this through
  the payload (build_request below always overwrites it with True).
- **No function describes the `posts.publish` (draft → live) endpoint.**
- A human makes the post public in Blogger, then records it with `mark-published`.

Blogger API v3:
  POST /blogs/{blogId}/posts?isDraft=true   → {id, url, status: "DRAFT", ...}
"""
from __future__ import annotations

PLATFORM = "blogger"


def build_request(payload: dict) -> dict:
    """Canonical payload → Blogger draft-creation request (no token or blogId).

    The transport fills in blogId and the token to build the real URL and headers.
    This only describes what to upload. `is_draft` is always True regardless of the
    payload — no path to publishing exists.
    """
    title = payload.get("title")
    if not title:
        raise ValueError("Failed to build blogger request: payload.title is empty")
    body = payload.get("text")
    if not body:
        raise ValueError("Failed to build blogger request: payload.text is empty")

    return {
        "platform": PLATFORM,
        "kind": "blogger#post",
        "title": title,
        "content": body,
        "labels": list(payload.get("labels") or []),
        # Constant True — always upload as a draft, whatever the payload holds.
        "is_draft": True,
    }


def parse_response(resp: dict) -> tuple[str, str]:
    """Blogger response → (post_id, url).

    A draft response has status DRAFT. LIVE means the draft contract was broken, so it is
    rejected — passing it silently would make our "not published" record diverge from reality.
    """
    post_id = resp.get("id")
    if not post_id:
        raise ValueError(f"blogger response has no post id: {resp!r}")

    status = resp.get("status")
    if status is not None and str(status).upper() != "DRAFT":
        raise ValueError(
            f"blogger response is not a draft (status={status!r}) — only a human may publish"
        )

    url = resp.get("url") or ""
    return str(post_id), url
