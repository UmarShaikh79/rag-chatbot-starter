"""The retrieval pipeline the orchestrator calls: retrieve -> (fuse) -> (rerank).

``build_pipeline`` reads ``retrieval.*`` and composes the steps:

    strategy: dense | bm25 | <yours>     one retriever
    strategy: hybrid                     hybrid.retrievers, fused with RRF (ADR 019)
    hyde.enabled                         wraps the retrievers named in hyde.apply_to
    rerank.enabled                       re-scores rerank.candidates hits, keeps top_k

``scorer`` names the gate score and every step that shapes it, e.g.
``rerank|rerank[rerank-v5.0-fast](hybrid(dense,bm25))``. Calibrations are stored per index and
scorer, so changing any step falls back to the provisional thresholds until you run
``chatbot calibrate`` again (ADR 011, ADR 021).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from chatbot.core.interfaces import (
    LLM,
    CalibrationStore,
    Embedder,
    Reranker,
    Retriever,
    VectorStore,
)
from chatbot.core.prompts import PromptRegistry
from chatbot.core.registry import registry
from chatbot.core.settings import Settings
from chatbot.core.types import Calibration, IndexInfo, ScoredChunk, Usage
from chatbot.observability.tracing import count, span
from chatbot.providers.rerankers import is_patient
from chatbot.retrieval.hybrid import HybridRetriever
from chatbot.retrieval.hyde import HydeRetriever

log = logging.getLogger(__name__)

# Set when this turn's rerank failed and the retrieval order was kept. Per task, so concurrent
# requests sharing one pipeline don't see each other's failures.
_fell_back: ContextVar[bool] = ContextVar("rerank_fell_back", default=False)


class RetrievalPipeline:
    name = "pipeline"

    def __init__(
        self,
        retriever: Retriever,
        store: VectorStore | None = None,
        gate: str = "dense",
        reranker: Reranker | None = None,
        rerank_candidates: int = 20,
        fallback: RetrievalPipeline | None = None,
    ) -> None:
        self.retriever = retriever
        self.store = store
        self.gate = gate
        self.reranker = reranker
        self.rerank_candidates = rerank_candidates
        # The same retrieval without the reranker: its gate, scorer and calibration judge a
        # turn whose rerank failed. None: a rerank failure fails the request.
        self.fallback = fallback

    @property
    def signature(self) -> str:
        inner = getattr(self.retriever, "signature", self.retriever.name)
        if self.reranker is not None:
            return f"{self.reranker.signature}({inner})"
        return inner

    @property
    def scorer(self) -> str:
        """The calibration key: which score is gated, and how it was produced."""
        return f"{self.gate}|{self.signature}"

    async def index(self) -> IndexInfo | None:
        return await self.retriever.index() if hasattr(self.retriever, "index") else None

    async def calibration(self, index: IndexInfo | None = None) -> Calibration | None:
        index = index or await self.index()
        if index is None or not isinstance(self.store, CalibrationStore):
            return None
        return await self.store.get_calibration(index, self.scorer)

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        _fell_back.set(False)
        n = max(k, self.rerank_candidates) if self.reranker is not None else k
        hits, usage = await self.retriever.retrieve(query, n, filters)
        if self.reranker is not None and hits:
            attrs = {
                "chatbot.rerank.model": self.reranker.signature,
                "chatbot.rerank.candidates": len(hits),
                "chatbot.rerank.top_n": k,
            }
            with span("rerank", attrs=attrs) as s:
                try:
                    hits, rerank_usage = await self.reranker.rerank(query, hits, k)
                except Exception as exc:
                    if self.fallback is None or is_patient():
                        raise
                    s.fail(exc)
                    s.set({"chatbot.rerank.fallback": True})
                    log.warning(
                        "rerank failed; answering from the retrieval order",
                        extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    count("chatbot.rerank.fallbacks", 1)
                    _fell_back.set(True)
                    return hits[:k], usage
            usage = usage.add(rerank_usage)
        return hits[:k], usage

    def used(self) -> RetrievalPipeline:
        """The pipeline whose scores this turn's hits carry: ``fallback`` after a failed
        rerank, else this one. Read its ``gate``, ``scorer`` and ``calibration``."""
        return self.fallback if _fell_back.get() and self.fallback is not None else self


def build_pipeline(
    settings: Settings,
    embedder: Embedder,
    store: VectorStore,
    alias: str,
    llms: dict[str, LLM] | None = None,
    prompts: PromptRegistry | None = None,
) -> RetrievalPipeline:
    cfg = settings.retrieval
    llms = llms or {}

    def one(name: str) -> Retriever:
        retriever: Retriever = registry.create(
            "retriever", name, embedder, store, alias, settings=settings
        )
        if cfg.hyde.enabled and name in cfg.hyde.apply_to:
            if prompts is None or cfg.hyde.model_role not in llms:
                raise ValueError("hyde needs prompts and the configured model role")
            retriever = HydeRetriever(
                retriever,
                llms[cfg.hyde.model_role],
                prompts,
                cfg.hyde.prompt,
                cfg.hyde.include_query,
            )
        return retriever

    if cfg.strategy == "hybrid":
        h = cfg.hybrid
        retriever: Retriever = HybridRetriever(
            [one(n) for n in h.retrievers], h.candidates, h.rrf_k, h.weights
        )
    else:
        retriever = one(cfg.strategy)
    reranker = None
    fallback = None
    if cfg.rerank.enabled:
        profile = settings.reranker_profile()
        reranker = registry.create("reranker", profile.provider, profile)
        if cfg.rerank.fallback:
            fallback = RetrievalPipeline(retriever, store, gate=cfg.gate_signal(rerank=False))
    return RetrievalPipeline(
        retriever,
        store,
        gate=cfg.gate_signal(),
        reranker=reranker,
        rerank_candidates=cfg.rerank.candidates,
        fallback=fallback,
    )
