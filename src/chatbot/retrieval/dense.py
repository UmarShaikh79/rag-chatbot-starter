from __future__ import annotations

import logging
from typing import Any

from chatbot.core.errors import IndexMismatchError
from chatbot.core.interfaces import Embedder, VectorStore
from chatbot.core.registry import registry
from chatbot.core.types import IndexInfo, ScoredChunk, Usage

log = logging.getLogger(__name__)


async def check_index(store: VectorStore, alias: str, embedder: Embedder) -> IndexInfo | None:
    """The index behind ``alias``, refusing one built with a different embedder (ADR 005)."""
    info = await store.get_index(alias)
    if info is not None and (
        info.embedding_model != embedder.model_id or info.dims != embedder.dims
    ):
        raise IndexMismatchError(
            f"Index '{alias}' was built with {info.embedding_model} ({info.dims} dims); "
            f"the configured embedder is {embedder.model_id} ({embedder.dims} dims). "
            "It will not be searched: run `chatbot reindex` and `chatbot index activate` "
            "(ADR 023), or switch models.embed back."
        )
    return info


class DenseRetriever:
    name = "dense"
    signature = "dense"

    def __init__(self, embedder: Embedder, store: VectorStore, alias: str) -> None:
        self.embedder = embedder
        self.store = store
        self.alias = alias

    async def index(self) -> IndexInfo | None:
        return await check_index(self.store, self.alias, self.embedder)

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        info = await self.index()
        if info is None:
            log.warning(
                "index does not exist yet; ingest documents first", extra={"alias": self.alias}
            )
            return [], Usage()
        vector, usage = await self.embedder.embed_query(query)
        hits = await self.store.search(info, vector, k, filters)
        for h in hits:  # stores written before milestone 3 may not fill scores
            h.scores.setdefault("dense", h.score)
        return hits, usage


@registry.register("retriever", "dense")
def make_dense(embedder: Embedder, store: VectorStore, alias: str, **_: Any) -> DenseRetriever:
    return DenseRetriever(embedder, store, alias)
