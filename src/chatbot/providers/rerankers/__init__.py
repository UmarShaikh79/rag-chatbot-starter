"""Rerankers re-score retrieved hits against the query (retrieval.rerank).

Each records its score under ``scores["rerank"]`` and sets ``score`` to it, keeping the
retrieval scores it was given. A reranker's scores live on their own scale, so enabling
one (or switching model) needs a new calibration (ADR 011).
"""

from __future__ import annotations

from chatbot.core.types import ScoredChunk


def apply_scores(hits: list[ScoredChunk], scores: list[float], top_n: int) -> list[ScoredChunk]:
    out = [
        h.model_copy(
            update={
                "score": float(s),
                "retriever": "rerank",
                "scores": {**h.scores, "rerank": float(s)},
            }
        )
        for h, s in zip(hits, scores, strict=True)
    ]
    out.sort(key=lambda h: (-h.score, h.chunk.id))
    return out[:top_n]
