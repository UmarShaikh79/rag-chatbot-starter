"""pgvector store (default). Needs pgvector >= 0.8 for iterative index scans (ADR 012).

Layout: one ``chunks`` table holds every physical index. Each index gets a partial HNSW
index on ``(embedding::vector(dims)) WHERE index_id = N``; queries that use the same
expression and predicate can use it.

Filtered search:
* Filters that match few rows (<= ``exact_scan_threshold``) run an exact scan, ordering by
  the uncast column so the planner cannot pick the HNSW index.
* Other filters use HNSW with ``hnsw.iterative_scan`` so the scan keeps going until it has
  ``k`` rows that pass the filter, capped by ``hnsw.max_scan_tuples``. With
  ``relaxed_order`` results may be slightly out of order, so they are re-sorted here.

Keyword search (ADR 020): every chunk also stores ``to_tsvector(text_config, text)`` and its
length, and ``keyword_search`` scores Okapi BM25 in SQL with a GIN index for candidates. No
extension needed. For tens of millions of chunks, swap in a dedicated engine (ParadeDB
``pg_search``, OpenSearch) behind the same ``keyword_search`` method.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg_pool import AsyncConnectionPool

from chatbot.core.errors import IndexMismatchError
from chatbot.core.registry import registry
from chatbot.core.settings import Bm25Options, HnswSection, Settings
from chatbot.core.types import Calibration, Chunk, IndexInfo, ParsedDocument, ScoredChunk
from chatbot.db import vector_literal

log = logging.getLogger(__name__)


def physical_name(alias: str, model: str, dims: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
    return f"{alias}__{slug}__{dims}"


class PgVectorStore:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        hnsw: HnswSection,
        exact_scan_threshold: int = 2000,
        text_config: str = "english",
        bm25: Bm25Options | None = None,
    ) -> None:
        self.pool = pool
        self.hnsw = hnsw
        self.exact_scan_threshold = exact_scan_threshold
        self.text_config = text_config  # for new indexes; existing ones keep their own
        self.bm25 = bm25 or Bm25Options()

    # ------------------------------------------------------------------ indexes

    @staticmethod
    def _info(row: dict[str, Any], alias: str) -> IndexInfo:
        return IndexInfo(
            id=row["id"],
            name=row["name"],
            alias=alias,
            embedding_model=row["embedding_model"],
            dims=row["dims"],
            chunker_version=row["chunker_version"],
            text_config=row.get("text_config") or "english",
        )

    async def get_index(self, alias: str) -> IndexInfo | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT v.* FROM index_aliases a JOIN vector_indexes v ON v.id = a.index_id
                   WHERE a.alias = %s""",
                (alias,),
            )
            row = await cur.fetchone()
        return self._info(row, alias) if row else None

    async def ensure_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str
    ) -> IndexInfo:
        current = await self.get_index(alias)
        if current is not None:
            if current.embedding_model != embedding_model or current.dims != dims:
                raise IndexMismatchError(
                    f"Index alias '{alias}' was built with {current.embedding_model} "
                    f"({current.dims} dims) but the configured embedder is {embedding_model} "
                    f"({dims} dims). Point vector_store.index at a new alias and re-ingest."
                )
            return current
        name = physical_name(alias, embedding_model, dims)
        async with self.pool.connection() as conn, conn.transaction():
            # Serialise concurrent creators of the same alias.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            cur = await conn.execute(
                """INSERT INTO vector_indexes (name, embedding_model, dims, chunker_version,
                                              text_config)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                (name, embedding_model, dims, chunker_version, self.text_config),
            )
            index_id = (await cur.fetchone())["id"]
            m, efc = int(self.hnsw.m), int(self.hnsw.ef_construction)
            await conn.execute(
                f"""CREATE INDEX IF NOT EXISTS chunks_hnsw_{index_id} ON chunks
                    USING hnsw ((embedding::vector({int(dims)})) vector_cosine_ops)
                    WITH (m = {m}, ef_construction = {efc})
                    WHERE index_id = {int(index_id)}"""
            )
            await conn.execute(
                """INSERT INTO index_aliases (alias, index_id) VALUES (%s, %s)
                   ON CONFLICT (alias) DO NOTHING""",
                (alias, index_id),
            )
        # Re-read through the alias: another process may have won the race.
        return await self.ensure_index(alias, embedding_model, dims, chunker_version)

    # ------------------------------------------------------------------ documents

    async def document_state(self, index: IndexInfo, document_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT content_hash, chunker_version, embedding_model FROM documents
                   WHERE index_id = %s AND id = %s""",
                (index.id, document_id),
            )
            return await cur.fetchone()

    async def replace_document(
        self,
        index: IndexInfo,
        document: ParsedDocument,
        chunks: list[Chunk],
        vectors: list[list[float]],
        user_id: str | None = None,
    ) -> None:
        """Atomically swap a document's chunks (ADR 013): readers see old or new, never both."""
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        for v in vectors:
            if len(v) != index.dims:
                raise IndexMismatchError(f"Vector has {len(v)} dims, index expects {index.dims}")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO documents (index_id, id, source, uri, title, content_hash,
                                          chunker_version, embedding_model, user_id, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (index_id, id) DO UPDATE SET
                     source = EXCLUDED.source, uri = EXCLUDED.uri, title = EXCLUDED.title,
                     content_hash = EXCLUDED.content_hash,
                     chunker_version = EXCLUDED.chunker_version,
                     embedding_model = EXCLUDED.embedding_model,
                     user_id = EXCLUDED.user_id, metadata = EXCLUDED.metadata,
                     updated_at = now()""",
                (
                    index.id,
                    document.id,
                    document.source,
                    document.uri,
                    document.title,
                    document.content_hash,
                    index.chunker_version,
                    index.embedding_model,
                    user_id,
                    json.dumps(document.metadata),
                ),
            )
            await conn.execute(
                "DELETE FROM chunks WHERE index_id = %s AND document_id = %s",
                (index.id, document.id),
            )
            if chunks:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """INSERT INTO chunks (index_id, id, document_id, position, text,
                                               metadata, content_hash, embedding, tsv)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector,
                                   to_tsvector(%s::regconfig, %s))""",
                        [
                            (
                                index.id,
                                c.id,
                                document.id,
                                c.position,
                                c.text,
                                json.dumps(
                                    {**c.metadata, "source": document.source, "uri": document.uri}
                                ),
                                c.content_hash,
                                vector_literal(v),
                                index.text_config,
                                c.text,
                            )
                            for c, v in zip(chunks, vectors, strict=True)
                        ],
                    )
                await conn.execute(
                    """UPDATE chunks SET doc_len = (
                           SELECT coalesce(sum(coalesce(array_length(u.positions, 1), 1)), 0)
                           FROM unnest(chunks.tsv) u)
                       WHERE index_id = %s AND document_id = %s""",
                    (index.id, document.id),
                )

    async def delete_document(self, index: IndexInfo, document_id: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM documents WHERE index_id = %s AND id = %s", (index.id, document_id)
            )
            return cur.rowcount

    async def list_document_ids(self, index: IndexInfo, source: str) -> list[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM documents WHERE index_id = %s AND source = %s ORDER BY id",
                (index.id, source),
            )
            return [r["id"] for r in await cur.fetchall()]

    async def stats(self, index: IndexInfo) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT d.source, count(DISTINCT d.id) AS documents, count(c.id) AS chunks,
                          max(d.updated_at) AS updated_at
                   FROM documents d LEFT JOIN chunks c
                     ON c.index_id = d.index_id AND c.document_id = d.id
                   WHERE d.index_id = %s GROUP BY d.source ORDER BY d.source""",
                (index.id,),
            )
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ search

    @staticmethod
    def _where(index: IndexInfo, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["c.index_id = %s"]
        params: list[Any] = [index.id]
        doc = filters.get("document_id")
        if doc is not None:
            ids = doc if isinstance(doc, list) else [doc]
            clauses.append("c.document_id = ANY(%s)")
            params.append([str(i) for i in ids])
        meta = {k: v for k, v in filters.items() if k != "document_id"}
        if meta:
            clauses.append("c.metadata @> %s::jsonb")
            params.append(json.dumps(meta))
        return " AND ".join(clauses), params

    async def search(
        self, index: IndexInfo, vector: list[float], k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        if len(vector) != index.dims:
            raise IndexMismatchError(f"Query vector has {len(vector)} dims, index has {index.dims}")
        where, params = self._where(index, filters)
        qvec = vector_literal(vector)
        dims = int(index.dims)
        async with self.pool.connection() as conn, conn.transaction():
            exact = False
            if filters:
                cur = await conn.execute(
                    f"SELECT count(*) AS n FROM (SELECT 1 FROM chunks c WHERE {where} LIMIT %s) s",
                    [*params, self.exact_scan_threshold + 1],
                )
                exact = (await cur.fetchone())["n"] <= self.exact_scan_threshold
            if exact:
                distance = "c.embedding <=> %s::vector"
            else:
                distance = f"(c.embedding::vector({dims})) <=> %s::vector({dims})"
                await conn.execute(f"SET LOCAL hnsw.ef_search = {int(self.hnsw.ef_search)}")
                await conn.execute(f"SET LOCAL hnsw.iterative_scan = {self.hnsw.iterative_scan}")
                await conn.execute(
                    f"SET LOCAL hnsw.max_scan_tuples = {int(self.hnsw.max_scan_tuples)}"
                )
            cur = await conn.execute(
                f"""SELECT c.id, c.document_id, c.position, c.text, c.metadata, c.content_hash,
                           {distance} AS distance
                    FROM chunks c WHERE {where}
                    ORDER BY distance LIMIT %s""",
                [qvec, *params, k],
            )
            rows = await cur.fetchall()
        hits = [
            ScoredChunk(
                chunk=Chunk(
                    id=r["id"],
                    document_id=r["document_id"],
                    position=r["position"],
                    text=r["text"],
                    metadata=r["metadata"],
                    content_hash=r["content_hash"],
                ),
                score=1.0 - float(r["distance"]),
                retriever="dense",
                scores={"dense": 1.0 - float(r["distance"])},
            )
            for r in rows
        ]
        hits.sort(key=lambda h: h.score, reverse=True)  # relaxed_order may be out of order
        log.debug("vector search", extra={"hits": len(hits), "exact": exact, "k": k})
        return hits

    async def keyword_search(
        self, index: IndexInfo, query: str, k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        """Okapi BM25 over ``chunks.tsv``. Document frequencies and the average length are
        computed per query from the index, so they never go stale after re-ingestion."""
        where, params = self._where(index, filters)
        k1, b = float(self.bm25.k1), float(self.bm25.b)
        sql = f"""
            WITH q AS (
                SELECT DISTINCT u.lexeme
                FROM unnest(to_tsvector(%s::regconfig, %s)) u
                WHERE u.lexeme !~ '[\\\\]'
            ),
            stats AS (
                SELECT count(*)::float8 AS n, coalesce(avg(doc_len), 0)::float8 AS avgdl
                FROM chunks WHERE index_id = %s
            ),
            df AS (
                SELECT q.lexeme,
                       (SELECT count(*) FROM chunks d
                        WHERE d.index_id = %s AND d.tsv @@ quote_literal(q.lexeme)::tsquery
                       )::float8 AS df
                FROM q
            ),
            cand AS (
                SELECT c.* FROM chunks c
                WHERE {where}
                  AND c.tsv @@ (SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery FROM q)
            )
            SELECT cand.id, cand.document_id, cand.position, cand.text, cand.metadata,
                   cand.content_hash,
                   sum(ln(1 + (stats.n - df.df + 0.5) / (df.df + 0.5))
                       * (u.tf * ({k1} + 1))
                       / (u.tf + {k1} * (1 - {b} + {b} * cand.doc_len
                                           / greatest(stats.avgdl, 1)))) AS score
            FROM cand CROSS JOIN stats
            CROSS JOIN LATERAL (
                SELECT x.lexeme, coalesce(array_length(x.positions, 1), 1) AS tf
                FROM unnest(cand.tsv) x
            ) u
            JOIN df ON df.lexeme = u.lexeme
            GROUP BY cand.id, cand.document_id, cand.position, cand.text, cand.metadata,
                     cand.content_hash
            ORDER BY score DESC, cand.id
            LIMIT %s"""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                sql, [index.text_config, query, index.id, index.id, *params, k]
            )
            rows = await cur.fetchall()
        return [
            ScoredChunk(
                chunk=Chunk(
                    id=r["id"],
                    document_id=r["document_id"],
                    position=r["position"],
                    text=r["text"],
                    metadata=r["metadata"],
                    content_hash=r["content_hash"],
                ),
                score=float(r["score"]),
                retriever="bm25",
                scores={"bm25": float(r["score"])},
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ calibration

    async def get_calibration(self, index: IndexInfo, scorer: str) -> Calibration | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT scorer, min_score, min_hits, metrics, dataset, calibrated_at
                   FROM retrieval_calibrations WHERE index_id = %s AND scorer = %s""",
                (index.id, scorer),
            )
            row = await cur.fetchone()
        return Calibration(**row) if row else None

    async def save_calibration(self, index: IndexInfo, calibration: Calibration) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO retrieval_calibrations
                       (index_id, scorer, min_score, min_hits, metrics, dataset, calibrated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (index_id, scorer) DO UPDATE SET
                     min_score = EXCLUDED.min_score, min_hits = EXCLUDED.min_hits,
                     metrics = EXCLUDED.metrics, dataset = EXCLUDED.dataset,
                     calibrated_at = now()""",
                (
                    index.id,
                    calibration.scorer,
                    calibration.min_score,
                    calibration.min_hits,
                    json.dumps(calibration.metrics),
                    calibration.dataset,
                ),
            )

    async def list_calibrations(self, index: IndexInfo) -> list[Calibration]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT scorer, min_score, min_hits, metrics, dataset, calibrated_at
                   FROM retrieval_calibrations WHERE index_id = %s ORDER BY scorer""",
                (index.id,),
            )
            return [Calibration(**r) for r in await cur.fetchall()]


@registry.register("vector_store", "pgvector")
def make_pgvector_store(settings: Settings, pool: AsyncConnectionPool) -> PgVectorStore:
    vs = settings.vector_store
    return PgVectorStore(
        pool, vs.hnsw, vs.exact_scan_threshold, vs.text_search_config, settings.retrieval.bm25
    )
