"""Shared HTTP plumbing for the Ollama adapters (chat and embeddings).

Ollama runs models locally and serves them on http://localhost:11434. The adapters talk to
its native API (``/api/chat``, ``/api/embed``) with httpx, so no extra package is needed.
Ollama also has an OpenAI-compatible endpoint (``/v1``); pointing the ``openai`` provider's
``base_url`` there works too, but loses the options below (``keep_alive``, ``think``,
``num_ctx``) and the native error messages.

Base URL, in order: the profile's ``base_url``, the ``OLLAMA_BASE_URL`` env var, then
http://localhost:11434. ``api_key_env`` adds a bearer token, for Ollama's hosted API or a
proxy in front of your own server.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from chatbot.core.errors import ProviderError

DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaError(ProviderError):
    """Ollama answered with an error (model not pulled, server down, bad request)."""


def base_url(profile_url: str | None) -> str:
    return (profile_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def make_client(
    profile_url: str | None, api_key_env: str | None, timeout_s: float
) -> httpx.AsyncClient:
    headers = {}
    key = os.environ.get(api_key_env) if api_key_env else None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Generation can take a while on CPU; the read timeout covers the gap between chunks.
    return httpx.AsyncClient(
        base_url=base_url(profile_url),
        headers=headers,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
    )


def raise_for_error(resp: httpx.Response, model: str, body: str | None = None) -> None:
    if resp.status_code < 400:
        return
    text = body if body is not None else resp.text
    try:
        import json

        detail = json.loads(text).get("error", text)
    except ValueError:
        detail = text
    hint = ""
    if resp.status_code == 404 and "not found" in str(detail).lower():
        hint = f" Pull it first: `ollama pull {model}` (or `make ollama-pull` with Docker)."
    raise OllamaError(
        f"Ollama returned {resp.status_code} for {model}: {detail}.{hint}", resp.status_code
    )


async def post_json(client: httpx.AsyncClient, path: str, payload: dict[str, Any], model: str):
    try:
        resp = await client.post(path, json=payload)
    except httpx.ConnectError as exc:
        raise OllamaError(
            f"Cannot reach Ollama at {client.base_url}: {exc}. Start it (`ollama serve`, or "
            "`docker compose --profile ollama up`) or set OLLAMA_BASE_URL."
        ) from exc
    raise_for_error(resp, model)
    return resp.json()
