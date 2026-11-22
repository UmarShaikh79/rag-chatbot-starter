"""JWT / OIDC bearer tokens (ADR 017).

The API is a resource server: your app (or its identity provider: Auth0, Okta, Entra ID,
Keycloak, Cognito, Google, ...) signs users in and sends their access token here as
``Authorization: Bearer <jwt>``. This module verifies it:

- signature against the provider's JWKS (discovered from ``{issuer}/.well-known/
  openid-configuration`` unless ``jwks_url`` is set), keys cached for ``jwks_cache_s`` and
  refetched once when a token names an unknown ``kid`` (key rotation);
- ``iss``, ``aud``, ``exp`` (required), ``nbf`` and ``iat`` with ``leeway_s`` of clock skew;
- only the configured ``algorithms`` (``none`` is never accepted; HS256 only with a secret);
- ``required_scopes`` from the ``scope`` (space-separated) or ``scp`` claims.

The user ID comes from ``user_id_claim`` (``sub``), so callers can't pick another user's ID.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from chatbot.auth.principal import Principal
from chatbot.core.settings import JwtSection

_MIN_REFRESH_S = 30.0  # never refetch the JWKS more often than this on unknown kids


class TokenError(Exception):
    """The token is missing, malformed, expired or not trusted. The message is safe to log."""


def scopes_of(claims: dict[str, Any]) -> frozenset[str]:
    found: set[str] = set()
    for name in ("scope", "scp"):
        value = claims.get(name)
        if isinstance(value, str):
            found.update(value.split())
        elif isinstance(value, list):
            found.update(str(v) for v in value)
    return frozenset(found)


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("ey")


class JwtVerifier:
    def __init__(self, cfg: JwtSection, http: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.http = http
        self.algorithms = [a for a in cfg.algorithms if a.lower() != "none"]
        self.secret: str | None = None
        if cfg.hs256_secret_env:
            self.secret = os.environ.get(cfg.hs256_secret_env) or None
            if "HS256" in self.algorithms and not self.secret:
                raise RuntimeError(f"auth.jwt.hs256_secret_env: {cfg.hs256_secret_env} is empty")
            if self.secret is not None and len(self.secret) < 32:
                raise RuntimeError("the HS256 secret must be at least 32 characters")
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._jwks_url = cfg.jwks_url
        self._lock = asyncio.Lock()

    @property
    def uses_jwks(self) -> bool:
        return any(a != "HS256" for a in self.algorithms)

    # ------------------------------------------------------------------ keys

    async def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=10.0)
        return self.http

    async def _discover(self) -> str:
        if self._jwks_url:
            return self._jwks_url
        issuer = (self.cfg.issuer or "").rstrip("/")
        client = await self._client()
        resp = await client.get(f"{issuer}/.well-known/openid-configuration")
        resp.raise_for_status()
        meta = resp.json()
        if str(meta.get("issuer", "")).rstrip("/") != issuer:
            raise TokenError("OIDC discovery returned a different issuer")
        self._jwks_url = str(meta["jwks_uri"])
        return self._jwks_url

    async def _refresh(self, force: bool) -> None:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if self._keys and not force and age < self.cfg.jwks_cache_s:
                return
            if force and age < _MIN_REFRESH_S:
                return
            client = await self._client()
            resp = await client.get(await self._discover())
            resp.raise_for_status()
            keys: dict[str, PyJWK] = {}
            for raw in resp.json().get("keys", []):
                if raw.get("use", "sig") != "sig":
                    continue
                try:
                    key = PyJWK(raw)
                except jwt.PyJWTError:
                    continue  # unsupported key type; skip it rather than fail every request
                keys[str(raw.get("kid", ""))] = key
            self._keys = keys
            self._fetched_at = time.monotonic()

    async def _key_for(self, header: dict[str, Any]) -> Any:
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise TokenError(f"token algorithm {alg!r} is not allowed")
        if alg == "HS256":
            return self.secret
        kid = str(header.get("kid", ""))
        try:
            await self._refresh(force=False)
            if kid not in self._keys:
                await self._refresh(force=True)  # the provider may have rotated its keys
        except httpx.HTTPError as exc:
            raise TokenError("could not fetch the identity provider's signing keys") from exc
        key = self._keys.get(kid)
        if key is None and not kid and len(self._keys) == 1:
            key = next(iter(self._keys.values()))
        if key is None:
            raise TokenError("token signed with an unknown key")
        return key.key

    # ------------------------------------------------------------------ verify

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError("malformed token") from exc
        key = await self._key_for(header)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[header["alg"]],
                audience=self.cfg.audience,
                issuer=self.cfg.issuer,
                leeway=self.cfg.leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenError(f"invalid token: {type(exc).__name__}") from exc
        subject = claims.get(self.cfg.user_id_claim)
        if not isinstance(subject, str) or not subject:
            raise TokenError(f"token has no '{self.cfg.user_id_claim}' claim")
        scopes = scopes_of(claims)
        missing = [s for s in self.cfg.required_scopes if s not in scopes]
        if missing:
            raise TokenError(f"token lacks required scopes: {missing}")
        return Principal(
            method="jwt",
            subject=subject[:200],
            scopes=scopes,
            admin=self.cfg.admin_scope in scopes,
        )


def sign_dev_token(
    secret: str,
    subject: str,
    issuer: str,
    audience: str,
    ttl_s: int = 3600,
    scopes: list[str] | None = None,
) -> str:
    """An HS256 token for local development (``chatbot dev-token``). Not for production."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_s,
    }
    if scopes:
        claims["scope"] = " ".join(scopes)
    return jwt.encode(claims, secret, algorithm="HS256")
