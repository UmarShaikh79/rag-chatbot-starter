"""Cohere Rerank over plain HTTP (no SDK): POST {base_url}/v2/rerank.

Set COHERE_API_KEY (or the profile's api_key_env). Cohere bills per search, not per token,
so rerank cost is not included in ``usage.cost_usd``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from chatbot.core.errors import ChatbotError
from chatbot.core.registry import registry
from chatbot.core.settings import RerankerProfile
from chatbot.core.types import ScoredChunk, Usage
from chatbot.providers.rerankers import apply_scores

DEFAULT_BASE_URL = "https://api.cohere.com"


class CohereReranker:
    def __init__(self, profile: RerankerProfile, client: httpx.AsyncClient | None = None) -> None:
        self.model = profile.model
        self.signature = f"rerank[{profile.model}]"
        self.base_url = (profile.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = profile.api_key_env or "COHERE_API_KEY"
        self.params = dict(profile.params)
        self.client = client

    async def rerank(
        self, query: str, hits: list[ScoredChunk], top_n: int
    ) -> tuple[list[ScoredChunk], Usage]:
        if not hits:
            return [], Usage()
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ChatbotError(f"Set {self.api_key_env} to use the Cohere reranker")
        body: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": [h.chunk.text for h in hits],
            **self.params,
        }
        client = self.client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.post(
                f"{self.base_url}/v2/rerank",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self.client is None:
                await client.aclose()
        scores = [0.0] * len(hits)
        for item in data.get("results", []):
            scores[int(item["index"])] = float(item["relevance_score"])
        return apply_scores(hits, scores, top_n), Usage()


@registry.register("reranker", "cohere")
def make_cohere(profile: RerankerProfile) -> CohereReranker:
    return CohereReranker(profile)
