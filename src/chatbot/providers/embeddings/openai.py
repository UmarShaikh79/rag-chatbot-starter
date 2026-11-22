from __future__ import annotations

import os
from typing import Any

from chatbot.core.registry import registry
from chatbot.core.settings import ConfigError, EmbeddingProfile
from chatbot.core.types import Usage


class OpenAIEmbedder:
    def __init__(self, profile: EmbeddingProfile, client: Any | None = None) -> None:
        self.model_id = profile.model
        self.dims = profile.dims
        if client is None:
            from openai import AsyncOpenAI

            env = profile.api_key_env or "OPENAI_API_KEY"
            api_key = os.environ.get(env, "").strip()
            if not api_key:  # the SDK would raise with a long traceback
                raise ConfigError(
                    f"Set {env} for the OpenAI model {profile.model} (see .env.example), or "
                    "run CHATBOT_PROFILE=offline, which needs no key."
                )
            client = AsyncOpenAI(api_key=api_key, base_url=profile.base_url)
        self.client = client

    async def _embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        resp = await self.client.embeddings.create(
            model=self.model_id, input=texts, dimensions=self.dims
        )
        vectors = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        tokens = resp.usage.total_tokens if resp.usage else 0
        return vectors, Usage(embedding_tokens=tokens)

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()
        return await self._embed(texts)

    async def embed_query(self, text: str) -> tuple[list[float], Usage]:
        vectors, usage = await self._embed([text])
        return vectors[0], usage


@registry.register("embedder", "openai")
def make_openai_embedder(profile: EmbeddingProfile) -> OpenAIEmbedder:
    return OpenAIEmbedder(profile)
