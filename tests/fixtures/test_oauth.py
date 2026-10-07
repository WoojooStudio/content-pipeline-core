"""Fixture tests — Threads token issue and refresh (publisher/oauth.py).

No sockets — send is injected as a stub, so only request building and response parsing are checked.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT / "toolbelt") not in sys.path:
    sys.path.insert(0, str(_ROOT / "toolbelt"))

from publisher import oauth  # noqa: E402


class _Send:
    """Stub that records requests and returns a response per URL."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.sent: list[dict] = []

    def __call__(self, request: dict) -> dict:
        self.sent.append(request)
        for key, resp in self.responses.items():
            if key in request["url"]:
                return resp
        raise AssertionError(f"Unexpected request: {request['url']}")


class TestAuthorizeUrl:
    def test_includes_required_params(self):
        url = oauth.authorize_url("APP1", "https://localhost/cb")
        assert url.startswith("https://threads.net/oauth/authorize?")
        assert "client_id=APP1" in url
        assert "response_type=code" in url
        assert "threads_basic" in url and "threads_content_publish" in url

    def test_does_not_request_extra_scopes(self):
        """Only the two needed to publish — insights and replies get added when needed."""
        url = oauth.authorize_url("APP1", "https://localhost/cb")
        for extra in ("threads_manage_insights", "threads_manage_replies",
                      "threads_read_replies"):
            assert extra not in url

    def test_can_include_state(self):
        assert "state=xyz" in oauth.authorize_url("A", "https://l/cb", state="xyz")


class TestRequests:
    def test_short_lived_request_shape(self):
        r = oauth.short_lived_request("APP1", "SECRET", "CODE", "https://l/cb")
        assert r["method"] == "POST"
        assert r["url"] == "https://graph.threads.net/oauth/access_token"
        assert r["data"]["grant_type"] == "authorization_code"
        assert r["data"]["code"] == "CODE"

    def test_long_lived_request_shape(self):
        r = oauth.long_lived_request("SECRET", "SHORT")
        assert r["method"] == "GET"
        assert r["url"] == "https://graph.threads.net/access_token"
        assert r["params"]["grant_type"] == "th_exchange_token"

    def test_refresh_request_shape(self):
        r = oauth.refresh_request("LONG")
        assert r["url"] == "https://graph.threads.net/refresh_access_token"
        assert r["params"]["grant_type"] == "th_refresh_token"

    def test_me_is_read_request(self):
        r = oauth.me_request("TOK")
        assert r["method"] == "GET" and r["url"].endswith("/v1.0/me")


class TestParse:
    def test_parse_short_lived(self):
        assert oauth.parse_short_lived({"access_token": "S", "user_id": 42}) == ("S", "42")

    def test_rejects_missing_token(self):
        with pytest.raises(oauth.OAuthError, match="access_token"):
            oauth.parse_short_lived({"user_id": 1})

    def test_rejects_missing_user_id(self):
        with pytest.raises(oauth.OAuthError, match="user_id"):
            oauth.parse_short_lived({"access_token": "S"})

    def test_error_message_does_not_leak_secrets(self):
        """Secrets and tokens must never appear in failure logs."""
        with pytest.raises(oauth.OAuthError) as e:
            oauth.parse_short_lived({"access_token": "", "client_secret": "SUPERSECRET"})
        assert "SUPERSECRET" not in str(e.value)
        assert "***" in str(e.value)

    def test_parse_long_lived(self):
        assert oauth.parse_long_lived({"access_token": "L", "expires_in": 5184000}) == ("L", 5184000)

    def test_expires_in_is_optional(self):
        assert oauth.parse_long_lived({"access_token": "L"}) == ("L", None)


class TestObtainLongLived:
    def _send(self, **over):
        base = {
            "/oauth/access_token": {"access_token": "SHORT", "user_id": "9001"},
            "/access_token": {"access_token": "LONG", "expires_in": 5184000},
            "/me": {"id": "9001", "username": "brand_account"},
        }
        base.update(over)
        return _Send(base)

    def test_full_exchange(self):
        send = self._send()
        out = oauth.obtain_long_lived("APP", "SEC", "CODE", "https://l/cb", send=send)
        assert out["access_token"] == "LONG"
        assert out["account_id"] == "9001"
        assert out["username"] == "brand_account"

    def test_exchange_order(self):
        """Order: short-lived → long-lived → me check."""
        send = self._send()
        oauth.obtain_long_lived("APP", "SEC", "CODE", "https://l/cb", send=send)
        urls = [r["url"] for r in send.sent]
        assert "/oauth/access_token" in urls[0]
        assert urls[1].endswith("/access_token")
        assert urls[2].endswith("/me")

    def test_returns_token_even_if_me_fails(self):
        """A failed account check does not block issuance."""
        send = self._send(**{"/me": {}})
        out = oauth.obtain_long_lived("APP", "SEC", "CODE", "https://l/cb", send=send)
        assert out["access_token"] == "LONG" and out["username"] is None

    def test_stops_if_short_lived_fails(self):
        send = _Send({"/oauth/access_token": {"error": "bad code"}})
        with pytest.raises(oauth.OAuthError):
            oauth.obtain_long_lived("APP", "SEC", "CODE", "https://l/cb", send=send)
        assert len(send.sent) == 1  # does not proceed to the next step


class TestRefreshAndVerify:
    def test_refresh(self):
        send = _Send({"/refresh_access_token": {"access_token": "LONG2",
                                                "expires_in": 5184000}})
        assert oauth.refresh("LONG", send=send)["access_token"] == "LONG2"

    def test_verify_is_read_only(self):
        send = _Send({"/me": {"id": "9001", "username": "acct"}})
        out = oauth.verify("TOK", send=send)
        assert out == {"account_id": "9001", "username": "acct"}
        assert all(r["method"] == "GET" for r in send.sent)
