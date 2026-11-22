"""Local cross-encoder reranker (sentence-transformers). Install with ``uv sync --extra rerank``.

The model downloads from Hugging Face on first use and runs on CPU or GPU. It scores each
(query, chunk) pair jointly, which is slower than embeddings but much sharper; keep
``retrieval.rerank.candidates`` around 20 to 50.
"""

from __future__ import annotations

import asyncio
from typing import Any

from chatbot.core.errors import ChatbotError
from chatbot.core.registry import registry
from chatbot.core.settings import RerankerProfile
from chatbot.core.types import ScoredChunk, Usage
from chatbot.providers.rerankers import apply_scores


class CrossEncoderReranker:
    def __init__(self, profile: RerankerProfile, model: Any | None = None) -> None:
        self.model_name = profile.model
        self.signature = f"rerank[{profile.model}]"
        self.params = dict(profile.params)
        self._model = model

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise ChatbotError(
                    "The cross_encoder reranker needs the rerank extra: uv sync --extra rerank"
                ) from exc
            self._model = CrossEncoder(self.model_name, **self.params)
        return self._model

    async def rerank(
        self, query: str, hits: list[ScoredChunk], top_n: int
    ) -> tuple[list[ScoredChunk], Usage]:
        if not hits:
            return [], Usage()
        model = await asyncio.to_thread(self._load)
        pairs = [(query, h.chunk.text) for h in hits]
        scores = await asyncio.to_thread(model.predict, pairs)
        return apply_scores(hits, [float(s) for s in scores], top_n), Usage()


@registry.register("reranker", "cross_encoder")
def make_cross_encoder(profile: RerankerProfile) -> CrossEncoderReranker:
    return CrossEncoderReranker(profile)
