"""Ollama embeddings adapter (native ``/api/embed``). Local, no API key (ADR 009).

The default local model is ``nomic-embed-text`` (768 dims). It was trained with task
prefixes and gives better retrieval with them, so the profile sets
``document_prefix: "search_document: "`` and ``query_prefix: "search_query: "``. Other
models want other prefixes or none (check the model card); the prefixes are part of how
vectors are made, so changing them needs a re-index (ADR 023), like changing the model.

Profile ``params``, all optional: ``document_prefix``, ``query_prefix``, ``keep_alive``,
``truncate`` (default true: Ollama cuts inputs longer than the model's context instead
of failing), ``timeout_s`` (default 120).
"""

from __future__ import annotations

from typing import Any

import httpx

from chatbot.core.registry import registry
from chatbot.core.settings import EmbeddingProfile
from chatbot.core.types import Usage
from chatbot.providers.ollama_client import OllamaError, make_client, post_json


class OllamaEmbedder:
    def __init__(self, profile: EmbeddingProfile, client: httpx.AsyncClient | None = None) -> None:
        self.model_id = profile.model
        self.dims = profile.dims
        params = dict(profile.params)
        self.document_prefix: str = params.pop("document_prefix", "") or ""
        self.query_prefix: str = params.pop("query_prefix", "") or ""
        self.keep_alive = params.pop("keep_alive", None)
        self.truncate = bool(params.pop("truncate", True))
        timeout = float(params.pop("timeout_s", 120))
        if params:
            raise ValueError(
                f"Unknown params for Ollama embedding model {profile.model}: {sorted(params)} "
                "(allowed: document_prefix, query_prefix, keep_alive, truncate, timeout_s)"
            )
        self.client = client or make_client(profile.base_url, profile.api_key_env, timeout)

    async def _embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "input": texts,
            "truncate": self.truncate,
        }
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        data = await post_json(self.client, "/api/embed", payload, self.model_id)
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise OllamaError(f"Ollama returned {len(vectors)} embeddings for {len(texts)} inputs")
        for v in vectors:
            if len(v) != self.dims:
                raise OllamaError(
                    f"{self.model_id} returned {len(v)} dims but its profile says {self.dims}; "
                    "fix dims in config/models.yaml (an index is built for one size)"
                )
        return vectors, Usage(embedding_tokens=int(data.get("prompt_eval_count") or 0))

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()
        return await self._embed([self.document_prefix + t for t in texts])

    async def embed_query(self, text: str) -> tuple[list[float], Usage]:
        vectors, usage = await self._embed([self.query_prefix + text])
        return vectors[0], usage


@registry.register("embedder", "ollama")
def make_ollama_embedder(profile: EmbeddingProfile) -> OllamaEmbedder:
    return OllamaEmbedder(profile)
