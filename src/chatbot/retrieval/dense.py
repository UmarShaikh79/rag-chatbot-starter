from __future__ import annotations

import logging
from typing import Any

from chatbot.core.errors import IndexMismatchError
from chatbot.core.interfaces import Embedder, VectorStore
from chatbot.core.registry import registry
from chatbot.core.types import IndexInfo, ScoredChunk, Usage

log = logging.getLogger(__name__)


class DenseRetriever:
    name = "dense"

    def __init__(self, embedder: Embedder, store: VectorStore, alias: str) -> None:
        self.embedder = embedder
        self.store = store
        self.alias = alias

    async def index(self) -> IndexInfo | None:
        info = await self.store.get_index(self.alias)
        if info is not None and (
            info.embedding_model != self.embedder.model_id or info.dims != self.embedder.dims
        ):
            raise IndexMismatchError(
                f"Index '{self.alias}' was built with {info.embedding_model} ({info.dims} dims); "
                f"the configured embedder is {self.embedder.model_id} ({self.embedder.dims} dims). "
                "Re-ingest into a new alias or switch the embedder back."
            )
        return info

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
        return await self.store.search(info, vector, k, filters), usage


@registry.register("retriever", "dense")
def make_dense(embedder: Embedder, store: VectorStore, alias: str) -> DenseRetriever:
    return DenseRetriever(embedder, store, alias)
