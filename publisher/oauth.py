"""oauth — issue and refresh Threads access tokens (not publishing; credential setup only).

Keeps the same boundary as the other publisher/ layers: **HTTP sits behind one injectable
layer**, and request building / response parsing are pure functions (testable without sockets).

Per Meta's documentation (checked 2026-08):
  authorize        GET  https://threads.net/oauth/authorize
                        ?client_id&redirect_uri&response_type=code&scope[&state]
  short-lived      POST https://graph.threads.net/oauth/access_token
                        client_id, client_secret, code, grant_type=authorization_code,
                        redirect_uri            → {access_token, user_id}   (code: 1 hour, single use)
  long-lived       GET  https://graph.threads.net/access_token
                        grant_type=th_exchange_token, client_secret, access_token
                                                → {access_token, expires_in} (60 days)
  refresh          GET  https://graph.threads.net/refresh_access_token
                        grant_type=th_refresh_token, access_token
                                                → extends 60 days (at least 24h old, before expiry)

**This module never stores the app secret or tokens.** The caller (cli) writes them to secrets/ with 0600.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable

AUTHORIZE_URL = "https://threads.net/oauth/authorize"
GRAPH = "https://graph.threads.net"

# Minimum scopes needed to publish. Do not widen beyond need (add insights when metrics arrive).
PUBLISH_SCOPES = ("threads_basic", "threads_content_publish")


class OAuthError(Exception):
    """Token issue/refresh failed (error response or missing field)."""


def authorize_url(
    client_id: str,
    redirect_uri: str,
    *,
    scopes: tuple[str, ...] = PUBLISH_SCOPES,
    state: str | None = None,
) -> str:
    """Authorization URL a human opens in a browser. Approving appends ?code=… to redirect_uri."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# --- request descriptions (pure) --------------------------------------------------------


def short_lived_request(client_id: str, client_secret: str, code: str,
                        redirect_uri: str) -> dict:
    return {
        "method": "POST",
        "url": f"{GRAPH}/oauth/access_token",
        "data": {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    }


def long_lived_request(client_secret: str, short_lived_token: str) -> dict:
    return {
        "method": "GET",
        "url": f"{GRAPH}/access_token",
        "params": {
            "grant_type": "th_exchange_token",
            "client_secret": client_secret,
            "access_token": short_lived_token,
        },
    }


def refresh_request(long_lived_token: str) -> dict:
    return {
        "method": "GET",
        "url": f"{GRAPH}/refresh_access_token",
        "params": {
            "grant_type": "th_refresh_token",
            "access_token": long_lived_token,
        },
    }


def me_request(access_token: str) -> dict:
    """Read request to check token validity and account (not publishing)."""
    return {
        "method": "GET",
        "url": f"{GRAPH}/v1.0/me",
        "params": {"fields": "id,username", "access_token": access_token},
    }


# --- response parsing (pure) --------------------------------------------------------


def parse_short_lived(resp: dict) -> tuple[str, str]:
    """→ (access_token, user_id). The Threads user_id is the publishing account_id."""
    token = resp.get("access_token")
    user_id = resp.get("user_id")
    if not token:
        raise OAuthError(f"short-lived response has no access_token: {_redact(resp)}")
    if not user_id:
        raise OAuthError(f"short-lived response has no user_id: {_redact(resp)}")
    return str(token), str(user_id)


def parse_long_lived(resp: dict) -> tuple[str, int | None]:
    """→ (access_token, expires_in seconds). Valid for 60 days."""
    token = resp.get("access_token")
    if not token:
        raise OAuthError(f"long-lived response has no access_token: {_redact(resp)}")
    exp = resp.get("expires_in")
    return str(token), (int(exp) if exp is not None else None)


def parse_me(resp: dict) -> tuple[str, str | None]:
    uid = resp.get("id")
    if not uid:
        raise OAuthError(f"me response has no id: {_redact(resp)}")
    return str(uid), resp.get("username")


def _redact(resp: dict) -> dict:
    """Mask values so tokens never leak into error messages."""
    return {k: ("***" if "token" in k.lower() or "secret" in k.lower() else v)
            for k, v in (resp or {}).items()}


# --- transport (injectable) --------------------------------------------------------


def http_send(request: dict) -> dict:
    """stdlib urllib transport — zero external deps. Tests inject a stub here."""
    method = request.get("method", "GET")
    url = request["url"]
    if method == "GET":
        url = f"{url}?{urllib.parse.urlencode(request.get('params') or {})}"
        req = urllib.request.Request(url, method="GET")
    else:
        body = urllib.parse.urlencode(request.get("data") or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (official API, https only)
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def obtain_long_lived(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    *,
    send: Callable[[dict], dict] = http_send,
) -> dict:
    """authorization code → 60-day long-lived token + account_id.

    Returns {"access_token", "account_id", "expires_in", "username"}
    (username is None if the lookup fails — the token itself is unaffected.)
    """
    short_token, user_id = parse_short_lived(
        send(short_lived_request(client_id, client_secret, code, redirect_uri))
    )
    long_token, expires_in = parse_long_lived(
        send(long_lived_request(client_secret, short_token))
    )
    username = None
    try:
        _uid, username = parse_me(send(me_request(long_token)))
    except (OAuthError, Exception):  # noqa: BLE001 — a failed check does not block issuance
        username = None
    return {
        "access_token": long_token,
        "account_id": user_id,
        "expires_in": expires_in,
        "username": username,
    }


def refresh(long_lived_token: str, *, send: Callable[[dict], dict] = http_send) -> dict:
    """Extend the 60-day token (at least 24h old, before expiry)."""
    token, expires_in = parse_long_lived(send(refresh_request(long_lived_token)))
    return {"access_token": token, "expires_in": expires_in}


def verify(access_token: str, *, send: Callable[[dict], dict] = http_send) -> dict:
    """Check the token is alive with a read — never publishes."""
    uid, username = parse_me(send(me_request(access_token)))
    return {"account_id": uid, "username": username}
