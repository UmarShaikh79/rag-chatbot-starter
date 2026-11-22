"""API key auth, and the request dependency that picks API key or JWT by ``auth.mode``.

The env var holds SHA-256 hashes, never the keys themselves. Create a key with
``chatbot hash-key``; send it as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
JWTs (``auth.mode: jwt`` or ``api_key_or_jwt``) arrive as ``Authorization: Bearer <jwt>``;
see ``chatbot.auth.jwt``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets

import httpx
from fastapi import HTTPException, Request, status

from chatbot.auth.jwt import JwtVerifier, KeysUnavailable, TokenError, looks_like_jwt
from chatbot.auth.principal import ANONYMOUS, Principal
from chatbot.core.settings import AuthSection

log = logging.getLogger(__name__)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> tuple[str, str]:
    key = "cb_" + secrets.token_urlsafe(32)
    return key, hash_key(key)


def presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    return bearer(request)


def bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class Authenticator:
    """Request dependency. Sets ``request.state.principal`` or answers 401."""

    def __init__(self, cfg: AuthSection, http: httpx.AsyncClient | None = None) -> None:
        self.mode = cfg.mode
        raw = os.environ.get(cfg.api_keys_env, "")
        self.hashes = [h.strip().lower() for h in raw.split(",") if h.strip()]
        if self.mode == "api_key" and not self.hashes:
            raise RuntimeError(
                f"auth.mode is api_key but {cfg.api_keys_env} is empty. "
                "Run `chatbot hash-key` and set the hash, or set auth.mode: none for local use."
            )
        self.jwt = JwtVerifier(cfg.jwt, http) if self.mode in ("jwt", "api_key_or_jwt") else None

    def _key_ok(self, key: str) -> bool:
        digest = hash_key(key)
        return any(hmac.compare_digest(digest, h) for h in self.hashes)

    async def authenticate(self, request: Request) -> Principal:
        if self.mode == "none":
            return ANONYMOUS
        if self.mode in ("api_key", "api_key_or_jwt"):
            header_key = request.headers.get("x-api-key")
            token = bearer(request)
            key = header_key.strip() if header_key else None
            if key is None and token and not (self.jwt and looks_like_jwt(token)):
                key = token
            if key and self._key_ok(key):
                return Principal(method="api_key", admin=True)
            if self.mode == "api_key" or (key and not token):
                raise self._unauthorized("invalid or missing API key")
        if self.jwt is not None:
            token = bearer(request)
            if not token:
                raise self._unauthorized("missing bearer token")
            try:
                return await self.jwt.verify(token)
            except TokenError as exc:
                log.info("token rejected", extra={"reason": str(exc)})
                raise self._unauthorized(str(exc)) from None
            except KeysUnavailable as exc:
                log.warning("token not checked", extra={"reason": str(exc)})
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(exc),
                    headers={"Retry-After": "30"},
                ) from None
        raise self._unauthorized("invalid or missing credentials")

    async def __call__(self, request: Request) -> None:
        request.state.principal = await self.authenticate(request)

    @staticmethod
    def _unauthorized(detail: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


# Milestone 1 name, kept so existing imports keep working.
ApiKeyAuth = Authenticator
