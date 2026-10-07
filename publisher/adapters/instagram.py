"""adapters/instagram — Instagram Graph API carousel request building and response parsing only.

**Adapters own no credentials or nonces.** Symmetric with the threads adapter — it turns the
canonical payload into an IG request and extracts media_id and permalink from the response.
The transport injects the token and account ID at the last moment.

Instagram Graph API carousel publishing (graph.facebook.com):
  1) for each image, POST /{ig_user_id}/media (is_carousel_item=true) → child creation_id
  2) POST /{ig_user_id}/media (media_type=CAROUSEL, children=[...]) → carousel creation_id
  3) POST /{ig_user_id}/media_publish (creation_id) → media_id
The adapter only describes the shape of these requests. Real HTTP, tokens and multi-step
orchestration belong to the transport.

Card-news payload contract: payload["images"] = list of image URLs (image_gen output, after
upload), payload["text"] = caption. Before image_gen is ready, payload may lack images, so
build_request rejects it explicitly with ValueError.
"""
from __future__ import annotations

PLATFORM = "instagram"

# IG carousels take 2–10 images. A single image is a plain image post (not a carousel).
_MIN_CAROUSEL = 2
_MAX_CAROUSEL = 10


def build_request(payload: dict) -> dict:
    """Canonical payload → Instagram carousel publish request (no token or account ID).

    The transport fills in ig_user_id and token to build the real multi-step requests.
    This only holds what to upload (image URL set and caption).
    """
    images = payload.get("images")
    if not images:
        raise ValueError(
            "Failed to build instagram request: payload.images is empty "
            "(a card-news image set is required — image_gen output)"
        )
    if not isinstance(images, list):
        raise ValueError(f"instagram payload.images must be a list: {type(images)}")
    if len(images) > _MAX_CAROUSEL:
        raise ValueError(
            f"instagram carousels allow at most {_MAX_CAROUSEL} images (got {len(images)})"
        )

    caption = payload.get("text") or ""
    media_type = "CAROUSEL" if len(images) >= _MIN_CAROUSEL else "IMAGE"
    return {
        "platform": PLATFORM,
        "media_type": media_type,
        "images": list(images),
        "caption": caption,
    }


def parse_response(resp: dict) -> tuple[str, str]:
    """Instagram Graph API publish response → (media_id, permalink).

    A media_publish response is at least {"id": "<media_id>"}. Use permalink if present,
    otherwise build one from media_id.
    """
    media_id = resp.get("id")
    if not media_id:
        raise ValueError(f"instagram response has no media id: {resp!r}")
    permalink = resp.get("permalink") or f"https://www.instagram.com/p/{media_id}"
    return str(media_id), permalink
