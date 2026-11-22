"""Hybrid retrieval: run several retrievers and fuse their rankings (ADR 019).

Reciprocal rank fusion scores a chunk ``sum(weight / (rrf_k + rank))`` over the rankings it
appears in. It uses ranks only, so it needs no score normalisation across retrievers whose
scores live on different scales (cosine vs BM25). The price: a fused score says nothing
about relevance on its own, so weak-retrieval thresholds never apply to it. Each hit keeps
the per-retriever scores it picked up in ``scores``, and ``retrieval.gate`` picks one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from chatbot.core.interfaces import Retriever
from chatbot.core.types import IndexInfo, ScoredChunk, Usage


def rrf_fuse(
    rankings: list[tuple[str, list[ScoredChunk]]],
    rrf_k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[ScoredChunk]:
    weights = weights or {}
    fused: dict[str, ScoredChunk] = {}
    totals: dict[str, float] = {}
    for name, hits in rankings:
        weight = weights.get(name, 1.0)
        for rank, hit in enumerate(hits, start=1):
            cid = hit.chunk.id
            if cid not in fused:
                fused[cid] = hit.model_copy(update={"scores": dict(hit.scores)})
            else:
                fused[cid].scores.update(hit.scores)
            totals[cid] = totals.get(cid, 0.0) + weight / (rrf_k + rank)
    out = []
    for cid, hit in fused.items():
        hit.scores["rrf"] = totals[cid]
        out.append(hit.model_copy(update={"score": totals[cid], "retriever": "hybrid"}))
    out.sort(key=lambda h: (-h.score, h.chunk.id))
    return out


class HybridRetriever:
    name = "hybrid"

    def __init__(
        self,
        retrievers: list[Retriever],
        candidates: int = 20,
        rrf_k: int = 60,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.retrievers = retrievers
        self.candidates = candidates
        self.rrf_k = rrf_k
        self.weights = weights or {}

    @property
    def signature(self) -> str:
        parts = ",".join(getattr(r, "signature", r.name) for r in self.retrievers)
        return f"hybrid({parts})"

    async def index(self) -> IndexInfo | None:
        for r in self.retrievers:
            if hasattr(r, "index"):
                return await r.index()
        return None

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        n = max(k, self.candidates)
        results = await asyncio.gather(*(r.retrieve(query, n, filters) for r in self.retrievers))
        usage = Usage()
        rankings = []
        for r, (hits, u) in zip(self.retrievers, results, strict=True):
            usage = usage.add(u)
            rankings.append((r.name, hits))
        return rrf_fuse(rankings, self.rrf_k, self.weights)[:k], usage
