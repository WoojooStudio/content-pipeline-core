"""credentials — resolves credentials_ref to secrets/<ref>.json.

Platform credentials (token, account ID) never leave secrets/. This module only reads the
file and wraps it in Credentials; there are no hard-coded tokens. If the file is missing
(token not ready) it raises CredentialsMissing — real publishing is only possible after a
human creates secrets/threads_main.json.

Expected format of secrets/threads_main.json:
    {"handle": "@brand_account", "account_id": "...", "access_token": "..."}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class CredentialsMissing(Exception):
    """secrets/<ref>.json is missing or lacks required fields (token not ready)."""


@dataclass(frozen=True)
class Credentials:
    """Platform credentials — injected by the transport just before sending. Never log or return them."""

    account_id: str
    access_token: str
    handle: str | None = None


def load_credentials(credentials_ref: str, secrets_root: Path) -> Credentials:
    """Read secrets/<credentials_ref>.json and return Credentials.

    Missing file or missing access_token/account_id → CredentialsMissing.
    """
    path = secrets_root / f"{credentials_ref}.json"
    if not path.exists():
        raise CredentialsMissing(
            f"Credentials file not found: {path} (create secrets/{credentials_ref}.json)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise CredentialsMissing(f"Cannot read credentials file: {path} ({e})") from e

    token = data.get("access_token")
    account_id = data.get("account_id")
    if not token or not account_id:
        raise CredentialsMissing(
            f"Credentials lack access_token/account_id: {path}"
        )
    return Credentials(
        account_id=str(account_id),
        access_token=str(token),
        handle=data.get("handle"),
    )
