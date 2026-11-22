"""Qdrant vector store (ADR 024). ``uv sync --extra qdrant``; ``vector_store.backend: qdrant``.

Split of responsibilities:

* Qdrant holds the vectors: one collection per physical index (cosine distance), one point
  per chunk with its text, position, document ID, user ID and metadata as payload.
* Postgres keeps the catalog, as it does for pgvector (``chatbot.stores.catalog``): index
  rows and aliases, document hashes and parsed text, calibrations. Aliases therefore work
  the same for both stores, and ``chatbot reindex`` can move an index from one store to the
  other.

Replacing a document (ADR 013): one ``batch_update_points`` call deletes the document's old
points and upserts the new ones, applied in order. Qdrant has no multi-operation
transactions, so a search that lands between the two steps can miss the document or see
part of the new version, but never a mix of old and new chunks. The document row in
Postgres is written after Qdrant succeeds, so a failure is retried on the next sync.

Filters: ``document_id`` (one or a list) and metadata keys. A scalar matches the key's
value (or any element when the stored value is a list); a list requires every element.

Not supported: ``keyword_search`` (BM25). Use ``retrieval.strategy: dense`` with Qdrant,
or keep pgvector for hybrid search. Qdrant's sparse vectors could fill this gap; that is
left to you.

Tests run Qdrant's in-process local mode (``location: ":memory:"``); CI also runs a real
server. Local mode ignores payload indexes and HNSW settings.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from chatbot.core.errors import ChatbotError, IndexMismatchError
from chatbot.core.registry import registry
from chatbot.core.settings import QdrantSection, Settings
from chatbot.core.types import Chunk, IndexInfo, ParsedDocument, ScoredChunk
from chatbot.stores.catalog import CatalogStore

log = logging.getLogger(__name__)

_NS = uuid.UUID("5b0f3f3e-9a57-4d3c-8f0e-2b1d5c3a9e10")  # point IDs: uuid5(_NS, chunk_id)
_KEYWORD_FIELDS = ("document_id", "user_id", "metadata.source")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_NS, chunk_id))


def _models() -> Any:
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ChatbotError(
            "vector_store.backend is qdrant but qdrant-client is not installed; run "
            "`uv sync --extra qdrant`"
        ) from exc
    return models


def make_client(cfg: QdrantSection) -> Any:
    try:
        from qdrant_client import AsyncQdrantClient
    except ImportError as exc:  # pragma: no cover
        raise ChatbotError(
            "vector_store.backend is qdrant but qdrant-client is not installed; run "
            "`uv sync --extra qdrant`"
        ) from exc
    if cfg.location:
        return AsyncQdrantClient(location=cfg.location)
    api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    return AsyncQdrantClient(
        url=os.environ.get(cfg.url_env) or cfg.url,
        api_key=api_key or None,
        prefer_grpc=cfg.prefer_grpc,
        timeout=cfg.timeout_s,
    )


class QdrantStore(CatalogStore):
    backend = "qdrant"

    def __init__(
        self,
        pool: AsyncConnectionPool,
        client: Any,
        cfg: QdrantSection | None = None,
        text_config: str = "english",
    ) -> None:
        super().__init__(pool, text_config)
        self.client = client
        self.cfg = cfg or QdrantSection()
        self.m = _models()

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------ collections

    async def _create_physical(self, info: IndexInfo, conn: AsyncConnection) -> None:
        m = self.m
        if await self.client.collection_exists(info.name):
            return
        await self.client.create_collection(
            info.name,
            vectors_config=m.VectorParams(
                size=int(info.dims), distance=m.Distance.COSINE, on_disk=self.cfg.on_disk
            ),
            hnsw_config=m.HnswConfigDiff(
                m=self.cfg.hnsw.m,
                ef_construct=self.cfg.hnsw.ef_construct,
                # Filterable HNSW: extra links per payload value keep filtered searches
                # (one document, one user) accurate without an exact-scan fallback.
                payload_m=self.cfg.hnsw.m,
            ),
        )
        for field in _KEYWORD_FIELDS:
            await self.client.create_payload_index(
                info.name, field, field_schema=m.PayloadSchemaType.KEYWORD
            )

    async def _drop_physical(self, info: IndexInfo, conn: AsyncConnection) -> None:
        if await self.client.collection_exists(info.name):
            await self.client.delete_collection(info.name)
        await conn.execute("DELETE FROM documents WHERE index_id = %s", (info.id,))

    def _check(self, index: IndexInfo) -> None:
        if index.backend != self.backend:
            raise IndexMismatchError(f"Index {index.name} is not a Qdrant index")

    # ------------------------------------------------------------------ documents

    def _doc_filter(self, document_id: str) -> Any:
        m = self.m
        return m.Filter(
            must=[m.FieldCondition(key="document_id", match=m.MatchValue(value=document_id))]
        )

    async def replace_document(
        self,
        index: IndexInfo,
        document: ParsedDocument,
        chunks: list[Chunk],
        vectors: list[list[float]],
        user_id: str | None = None,
    ) -> None:
        self._check(index)
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        for v in vectors:
            if len(v) != index.dims:
                raise IndexMismatchError(f"Vector has {len(v)} dims, index expects {index.dims}")
        m = self.m
        points = [
            m.PointStruct(
                id=point_id(c.id),
                vector=[float(x) for x in v],
                payload={
                    "chunk_id": c.id,
                    "document_id": document.id,
                    "position": c.position,
                    "text": c.text,
                    "content_hash": c.content_hash,
                    "user_id": user_id,
                    "metadata": {**c.metadata, "source": document.source, "uri": document.uri},
                },
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]
        ops: list[Any] = [
            m.DeleteOperation(delete=m.FilterSelector(filter=self._doc_filter(document.id)))
        ]
        size = self.cfg.upsert_batch
        ops += [
            m.UpsertOperation(upsert=m.PointsList(points=points[i : i + size]))
            for i in range(0, len(points), size)
        ]
        await self.client.batch_update_points(index.name, ops, wait=True)
        async with self.pool.connection() as conn:
            await self._upsert_document(conn, index, document, user_id)

    async def delete_document(self, index: IndexInfo, document_id: str) -> int:
        self._check(index)
        m = self.m
        await self.client.delete(
            index.name, m.FilterSelector(filter=self._doc_filter(document_id)), wait=True
        )
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM documents WHERE index_id = %s AND id = %s", (index.id, document_id)
            )
            return cur.rowcount

    async def forget_user_documents(self, user_id: str) -> int:
        """Delete a user's points from every Qdrant index. ``forget_user`` then deletes the
        document rows in Postgres, as it does for pgvector."""
        m = self.m
        flt = m.Filter(must=[m.FieldCondition(key="user_id", match=m.MatchValue(value=user_id))])
        deleted = 0
        for summary in await self.list_indexes():
            if summary.backend != self.backend:
                continue
            if not await self.client.collection_exists(summary.name):
                continue
            n = (await self.client.count(summary.name, count_filter=flt, exact=True)).count
            if n:
                await self.client.delete(summary.name, m.FilterSelector(filter=flt), wait=True)
            deleted += n
        return deleted

    async def stats(self, index: IndexInfo) -> list[dict[str, Any]]:
        rows = await super().stats(index)
        m = self.m
        for row in rows:
            flt = m.Filter(
                must=[
                    m.FieldCondition(key="metadata.source", match=m.MatchValue(value=row["source"]))
                ]
            )
            row["chunks"] = (await self.client.count(index.name, count_filter=flt)).count
        return rows

    # ------------------------------------------------------------------ search

    def _filter(self, filters: dict[str, Any]) -> Any:
        if not filters:
            return None
        m = self.m
        must: list[Any] = []
        doc = filters.get("document_id")
        if doc is not None:
            ids = [str(i) for i in (doc if isinstance(doc, list) else [doc])]
            must.append(m.FieldCondition(key="document_id", match=m.MatchAny(any=ids)))
        for key, value in filters.items():
            if key == "document_id":
                continue
            for item in value if isinstance(value, list) else [value]:
                must.append(m.FieldCondition(key=f"metadata.{key}", match=m.MatchValue(value=item)))
        return m.Filter(must=must)

    async def search(
        self, index: IndexInfo, vector: list[float], k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        self._check(index)
        if len(vector) != index.dims:
            raise IndexMismatchError(f"Query vector has {len(vector)} dims, index has {index.dims}")
        resp = await self.client.query_points(
            index.name,
            query=[float(x) for x in vector],
            query_filter=self._filter(filters),
            search_params=self.m.SearchParams(hnsw_ef=self.cfg.hnsw_ef),
            limit=k,
            with_payload=True,
        )
        hits = []
        for p in resp.points:
            pl = p.payload or {}
            score = float(p.score)  # cosine similarity, same scale as pgvector's 1 - distance
            hits.append(
                ScoredChunk(
                    chunk=Chunk(
                        id=pl["chunk_id"],
                        document_id=pl["document_id"],
                        position=pl["position"],
                        text=pl["text"],
                        metadata=pl.get("metadata") or {},
                        content_hash=pl["content_hash"],
                    ),
                    score=score,
                    retriever="dense",
                    scores={"dense": score},
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits


@registry.register("vector_store", "qdrant")
def make_qdrant_store(settings: Settings, pool: AsyncConnectionPool) -> QdrantStore:
    vs = settings.vector_store
    return QdrantStore(pool, make_client(vs.qdrant), vs.qdrant, vs.text_search_config)
