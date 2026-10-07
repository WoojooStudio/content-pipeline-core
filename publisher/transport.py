"""transport — injects credentials into adapter-built requests at the last moment and sends them.

transport = last-moment auth injection. Adapters never see tokens; only the transport receives
credentials and builds the real HTTP destination and headers.

- ThreadsTransport: real HTTP (graph.threads.net), stdlib urllib only (zero external deps).
  Token comes from credentials. If absent, CredentialsMissing at send time.
- FakeTransport: deterministic responses — for tests and dry runs. No token needed.

Transport is a typing.Protocol — structural typing, no runtime inheritance required.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Protocol

from .credentials import Credentials, CredentialsMissing

_THREADS_API = "https://graph.threads.net/v1.0"
_BLOGGER_API = "https://www.googleapis.com/blogger/v3"


class Transport(Protocol):
    """Request dict → response dict. Implementations own credentials (constructor-injected)."""

    def send(self, request: dict) -> dict: ...


class FakeTransport:
    """Test / dry-run transport — never publishes; returns deterministic responses.

    Records sent requests in `sent` so tests can inspect the payload.
    """

    def __init__(self, post_id: str = "MOCK_POST_1") -> None:
        self._post_id = post_id
        self.sent: list[dict] = []

    def send(self, request: dict) -> dict:
        self.sent.append(request)
        if request.get("platform") == "blogger":
            # Draft upload response — the adapter only accepts status=DRAFT (rejects live).
            return {
                "id": self._post_id,
                "url": f"https://example.blogspot.com/{self._post_id}",
                "status": "DRAFT",
            }
        return {"id": self._post_id, "permalink": f"https://www.threads.net/t/{self._post_id}"}


class ThreadsTransport:
    """Real Threads publishing — two steps (create container → publish). urllib only.

    If credentials is None, CredentialsMissing at send time (construction works without a token).
    """

    def __init__(self, credentials: Credentials | None) -> None:
        self._creds = credentials

    def send(self, request: dict) -> dict:
        if self._creds is None:
            raise CredentialsMissing("ThreadsTransport has no credentials (token not ready)")
        text = request["text"]
        media_type = request.get("media_type", "TEXT")
        account_id = self._creds.account_id
        token = self._creds.access_token

        # Step 1: create media container
        creation = self._post(
            f"{_THREADS_API}/{account_id}/threads",
            {"media_type": media_type, "text": text, "access_token": token},
        )
        creation_id = creation.get("id")
        if not creation_id:
            raise RuntimeError(f"Container creation failed: {creation!r}")

        # Step 2: publish
        published = self._post(
            f"{_THREADS_API}/{account_id}/threads_publish",
            {"creation_id": creation_id, "access_token": token},
        )
        return published

    @staticmethod
    def _post(url: str, params: dict) -> dict:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (official API, https only)
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


class BloggerTransport:
    """Real Blogger draft upload — `posts.insert?isDraft=true` only. urllib only.

    **There is no path to publishing.** No code in this class calls `posts.publish`, and
    the isDraft query is a constant true. A human makes the post public in Blogger, then
    records it with `mark-published`.

    Credentials are an OAuth access token + blog_id. If not ready, CredentialsMissing at
    send time — **real upload verification is deferred until Google OAuth is set up.**
    """

    def __init__(self, credentials: Credentials | None, blog_id: str | None = None) -> None:
        self._creds = credentials
        self._blog_id = blog_id

    def send(self, request: dict) -> dict:
        if self._creds is None:
            raise CredentialsMissing("BloggerTransport has no credentials (OAuth token not ready)")
        blog_id = self._blog_id or getattr(self._creds, "account_id", None)
        if not blog_id:
            raise CredentialsMissing("BloggerTransport has no blog_id")
        if not request.get("is_draft"):
            # The adapter always sets True, but block calls that bypass that layer here too.
            raise ValueError("blogger uploads must be drafts (isDraft=true)")

        body = {
            "kind": "blogger#post",
            "title": request["title"],
            "content": request["content"],
        }
        if request.get("labels"):
            body["labels"] = list(request["labels"])

        url = f"{_BLOGGER_API}/blogs/{blog_id}/posts?isDraft=true"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._creds.access_token}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (official API, https only)
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
