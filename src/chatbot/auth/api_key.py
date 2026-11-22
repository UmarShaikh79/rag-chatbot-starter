"""API key auth. The env var holds SHA-256 hashes, never the keys themselves.

Create a key with ``chatbot hash-key``; send it as ``X-API-Key: <key>`` or
``Authorization: Bearer <key>``. JWT/OIDC arrives in milestone 2.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request, status

from chatbot.core.settings import AuthSection


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> tuple[str, str]:
    key = "cb_" + secrets.token_urlsafe(32)
    return key, hash_key(key)


def presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class ApiKeyAuth:
    def __init__(self, cfg: AuthSection) -> None:
        self.mode = cfg.mode
        raw = os.environ.get(cfg.api_keys_env, "")
        self.hashes = [h.strip().lower() for h in raw.split(",") if h.strip()]
        if self.mode == "api_key" and not self.hashes:
            raise RuntimeError(
                f"auth.mode is api_key but {cfg.api_keys_env} is empty. "
                "Run `chatbot hash-key` and set the hash, or set auth.mode: none for local use."
            )

    async def __call__(self, request: Request) -> None:
        if self.mode == "none":
            return
        key = presented_key(request)
        if key:
            digest = hash_key(key)
            if any(hmac.compare_digest(digest, h) for h in self.hashes):
                return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
