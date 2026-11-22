"""Rerankers re-score retrieved hits against the query (retrieval.rerank).

Each records its score under ``scores["rerank"]`` and sets ``score`` to it, keeping the
retrieval scores it was given. A reranker's scores live on their own scale, so enabling
one (or switching model) needs a new calibration (ADR 011).

A hosted reranker can be rate-limited or down. Chat turns retry briefly and then fall back to
the retrieval order (``retrieval.rerank.fallback``). ``chatbot calibrate`` and ``chatbot eval``
run inside ``patient_reranking()``: they wait out rate limits and never fall back, so a
calibration is never computed from scores of the wrong scale.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from chatbot.core.errors import ChatbotError
from chatbot.core.types import ScoredChunk

_patient: ContextVar[bool] = ContextVar("rerank_patient", default=False)


class RerankError(ChatbotError):
    """The reranker could not score the hits (after its retries)."""


@contextmanager
def patient_reranking() -> Iterator[None]:
    """Inside the block: wait out rate limits (longer retries) and never fall back."""
    token = _patient.set(True)
    try:
        yield
    finally:
        _patient.reset(token)


def is_patient() -> bool:
    return _patient.get()


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
