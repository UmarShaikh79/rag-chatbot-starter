"""Index catalog in Postgres, shared by every vector store (ADR 023, ADR 024).

Postgres always runs (memory and jobs live there), so it also keeps the bookkeeping for
every index, whichever store holds the vectors:

* ``vector_indexes``: one row per physical index: embedding model, dims, chunker version,
  backend, status (``building`` or ``ready``).
* ``index_aliases``: the name in config (``vector_store.index``) points at one physical
  index, and remembers the one before it for rollback.
* ``documents``: content hash, chunker version and the parsed text of every document.
* ``retrieval_calibrations``: thresholds per index and scorer (ADR 021).

``CatalogStore`` implements everything that only touches those tables. A store subclasses
it and adds the vector parts: ``_create_physical``, ``_drop_physical``, ``replace_document``,
``delete_document`` and ``search``.

Re-indexing (``chatbot reindex``) builds a new physical index behind a staging alias
(``docs_next``), so the live alias keeps serving the old one. ``activate`` then moves the
alias in one transaction, and ``rollback`` moves it back.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from chatbot.core.errors import ChatbotError, IndexMismatchError
from chatbot.core.types import (
    Calibration,
    IndexInfo,
    IndexSummary,
    ParsedDocument,
    Section,
    StoredDocument,
)

STAGING_SUFFIX = "_next"


def physical_name(alias: str, model: str, dims: int, stamp: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
    name = f"{alias}__{slug}__{dims}"
    return f"{name}__{stamp}" if stamp else name


def staging_alias(alias: str) -> str:
    return f"{alias}{STAGING_SUFFIX}"


def mismatch_message(alias: str, info: IndexInfo, model: str, dims: int) -> str:
    return (
        f"Index alias '{alias}' points at {info.name}, built with {info.embedding_model} "
        f"({info.dims} dims); the configured embedder is {model} ({dims} dims). Vectors from "
        "different models are not comparable, so it will not be searched. Run "
        "`chatbot reindex` to build an index for the new model, then `chatbot index "
        "activate`; or switch models.embed back."
    )


class CatalogStore:
    """Base class: catalog operations in Postgres; vectors are up to the subclass."""

    backend = "base"

    def __init__(self, pool: AsyncConnectionPool, text_config: str = "english") -> None:
        self.pool = pool
        self.text_config = text_config  # for new indexes; existing ones keep their own

    # ------------------------------------------------------------------ hooks

    async def _create_physical(self, info: IndexInfo, conn: AsyncConnection) -> None:
        """Create the store-side index (an HNSW index, a collection). Must be idempotent."""

    async def _drop_physical(self, info: IndexInfo, conn: AsyncConnection) -> None:
        """Remove the store-side index and its vectors."""

    # ------------------------------------------------------------------ reading indexes

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
            backend=row.get("backend") or "pgvector",
        )

    async def resolve_any(self, alias: str) -> IndexInfo | None:
        """The index behind ``alias``, whichever store holds it."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT v.* FROM index_aliases a JOIN vector_indexes v ON v.id = a.index_id
                   WHERE a.alias = %s""",
                (alias,),
            )
            row = await cur.fetchone()
        return self._info(row, alias) if row else None

    async def get_index(self, alias: str) -> IndexInfo | None:
        info = await self.resolve_any(alias)
        if info is not None and info.backend != self.backend:
            raise IndexMismatchError(
                f"Index alias '{alias}' points at {info.name}, which lives in the "
                f"{info.backend} store, but vector_store.backend is {self.backend}. Run "
                "`chatbot reindex` to build it in this store, then `chatbot index activate`."
            )
        return info

    async def _by_name(self, name: str) -> IndexInfo | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM vector_indexes WHERE name = %s", (name,))
            row = await cur.fetchone()
        return self._info(row, "") if row else None

    async def list_indexes(self) -> list[IndexSummary]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT v.*,
                     coalesce((SELECT array_agg(a.alias ORDER BY a.alias) FROM index_aliases a
                               WHERE a.index_id = v.id), '{}') AS aliases,
                     coalesce((SELECT array_agg(a.alias ORDER BY a.alias) FROM index_aliases a
                               WHERE a.previous_index_id = v.id), '{}') AS previous_of,
                     (SELECT count(*) FROM documents d WHERE d.index_id = v.id) AS documents
                   FROM vector_indexes v ORDER BY v.created_at, v.id"""
            )
            rows = await cur.fetchall()
        return [
            IndexSummary(
                name=r["name"],
                backend=r["backend"],
                embedding_model=r["embedding_model"],
                dims=r["dims"],
                chunker_version=r["chunker_version"],
                status=r["status"],
                aliases=list(r["aliases"]),
                previous_of=list(r["previous_of"]),
                documents=r["documents"],
                created_at=r["created_at"],
                ready_at=r["ready_at"],
                build_report=r["build_report"] or {},
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ creating indexes

    async def _insert_index(
        self,
        conn: AsyncConnection,
        name: str,
        alias: str,
        embedding_model: str,
        dims: int,
        chunker_version: str,
        status: str,
    ) -> IndexInfo:
        cur = await conn.execute(
            """INSERT INTO vector_indexes (name, embedding_model, dims, chunker_version,
                                          text_config, backend, status, ready_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s = 'ready' THEN now() END)
               ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
               RETURNING *""",
            (
                name,
                embedding_model,
                dims,
                chunker_version,
                self.text_config,
                self.backend,
                status,
                status,
            ),
        )
        info = self._info(await cur.fetchone(), alias)
        if info.backend != self.backend:
            raise ChatbotError(f"Index {name} already exists in the {info.backend} store")
        await self._create_physical(info, conn)
        return info

    async def ensure_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str
    ) -> IndexInfo:
        """The index behind ``alias``; created (and the alias pointed at it) on first use.
        Refuses an alias whose index was built with another embedding model (ADR 005)."""
        current = await self.get_index(alias)
        if current is not None:
            if current.embedding_model != embedding_model or current.dims != dims:
                raise IndexMismatchError(mismatch_message(alias, current, embedding_model, dims))
            return current
        async with self.pool.connection() as conn, conn.transaction():
            # Serialise concurrent creators of the same alias.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            cur = await conn.execute("SELECT 1 FROM index_aliases WHERE alias = %s", (alias,))
            if await cur.fetchone() is None:
                info = await self._insert_index(
                    conn,
                    physical_name(alias, embedding_model, dims),
                    alias,
                    embedding_model,
                    dims,
                    chunker_version,
                    "ready",
                )
                await conn.execute(
                    "INSERT INTO index_aliases (alias, index_id) VALUES (%s, %s)",
                    (alias, info.id),
                )
        # Re-read through the alias: another process may have won the race.
        return await self.ensure_index(alias, embedding_model, dims, chunker_version)

    async def create_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str, staging: str
    ) -> IndexInfo:
        """A new, empty index in ``building`` state, reachable through ``staging`` only. The
        live alias is not touched until ``activate``."""
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        name = physical_name(alias, embedding_model, dims, stamp)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            info = await self._insert_index(
                conn, name, staging, embedding_model, dims, chunker_version, "building"
            )
            await conn.execute(
                """INSERT INTO index_aliases (alias, index_id) VALUES (%s, %s)
                   ON CONFLICT (alias) DO UPDATE SET index_id = EXCLUDED.index_id,
                                                     updated_at = now()""",
                (staging, info.id),
            )
        return info

    async def mark_ready(self, index: IndexInfo, report: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """UPDATE vector_indexes SET status = 'ready', ready_at = now(),
                          build_report = %s WHERE id = %s""",
                (json.dumps(report, default=str), index.id),
            )

    async def save_build_report(self, index: IndexInfo, report: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE vector_indexes SET build_report = %s WHERE id = %s",
                (json.dumps(report, default=str), index.id),
            )

    # ------------------------------------------------------------------ switching

    async def activate(self, alias: str, staging: str, force: bool = False) -> IndexInfo:
        """Point ``alias`` at the index behind ``staging`` in one transaction. Readers see
        the old index or the new one, never a mix. The old one is kept for rollback."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            cur = await conn.execute(
                """SELECT v.* FROM index_aliases a JOIN vector_indexes v ON v.id = a.index_id
                   WHERE a.alias = %s""",
                (staging,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ChatbotError(
                    f"Nothing to activate: alias '{staging}' does not exist. Run "
                    "`chatbot reindex` first."
                )
            if row["status"] != "ready" and not force:
                raise ChatbotError(
                    f"Index {row['name']} is still building or its build left documents "
                    "behind (see `chatbot index list`). Finish `chatbot reindex`, or pass "
                    "--force to switch anyway."
                )
            if row["backend"] != self.backend:
                raise ChatbotError(
                    f"Index {row['name']} lives in the {row['backend']} store but "
                    f"vector_store.backend is {self.backend}"
                )
            await conn.execute(
                """INSERT INTO index_aliases (alias, index_id) VALUES (%s, %s)
                   ON CONFLICT (alias) DO UPDATE SET
                     previous_index_id = CASE WHEN index_aliases.index_id = EXCLUDED.index_id
                                              THEN index_aliases.previous_index_id
                                              ELSE index_aliases.index_id END,
                     index_id = EXCLUDED.index_id, updated_at = now()""",
                (alias, row["id"]),
            )
            await conn.execute("DELETE FROM index_aliases WHERE alias = %s", (staging,))
        return self._info(row, alias)

    async def rollback(self, alias: str) -> IndexInfo:
        """Swap ``alias`` back to the index it pointed at before the last switch."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            cur = await conn.execute(
                "SELECT index_id, previous_index_id FROM index_aliases WHERE alias = %s",
                (alias,),
            )
            row = await cur.fetchone()
            if row is None or row["previous_index_id"] is None:
                raise ChatbotError(f"Alias '{alias}' has no previous index to roll back to")
            await conn.execute(
                """UPDATE index_aliases SET index_id = %s, previous_index_id = %s,
                          updated_at = now() WHERE alias = %s""",
                (row["previous_index_id"], row["index_id"], alias),
            )
        info = await self.resolve_any(alias)
        assert info is not None
        return info

    async def remove_alias(self, alias: str) -> list[str]:
        """Delete ``alias`` and its rollback pointer. The indexes it pointed at stay (drop them
        with ``drop_index`` once nothing else points at them). Returns their names, current
        first."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (alias,))
            cur = await conn.execute(
                "SELECT index_id, previous_index_id FROM index_aliases WHERE alias = %s",
                (alias,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ChatbotError(f"No alias named '{alias}' (see `chatbot index list`)")
            ids = [i for i in (row["index_id"], row["previous_index_id"]) if i is not None]
            cur = await conn.execute(
                "SELECT id, name FROM vector_indexes WHERE id = ANY(%s)", (ids,)
            )
            names = {r["id"]: r["name"] for r in await cur.fetchall()}
            await conn.execute("DELETE FROM index_aliases WHERE alias = %s", (alias,))
        return [names[i] for i in ids if i in names]

    async def drop_index(self, name: str) -> None:
        """Delete an index, its documents, vectors and calibrations. Refuses one that an
        alias points at; a rollback target can be dropped (rollback is then impossible)."""
        info = await self._by_name(name)
        if info is None:
            raise ChatbotError(f"No index named {name}")
        if info.backend != self.backend:
            raise ChatbotError(
                f"Index {name} lives in the {info.backend} store; drop it with "
                f"vector_store.backend set to {info.backend}"
            )
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT alias FROM index_aliases WHERE index_id = %s ORDER BY alias", (info.id,)
            )
            aliases = [r["alias"] for r in await cur.fetchall()]
            if aliases:
                raise ChatbotError(
                    f"Index {name} is in use by alias {', '.join(aliases)}; activate or roll "
                    "back to another index first"
                )
            await self._drop_physical(info, conn)
            await conn.execute("DELETE FROM vector_indexes WHERE id = %s", (info.id,))

    # ------------------------------------------------------------------ documents

    async def document_state(self, index: IndexInfo, document_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT content_hash, chunker_version, embedding_model FROM documents
                   WHERE index_id = %s AND id = %s""",
                (index.id, document_id),
            )
            return await cur.fetchone()

    async def _upsert_document(
        self,
        conn: AsyncConnection,
        index: IndexInfo,
        document: ParsedDocument,
        user_id: str | None,
    ) -> None:
        await conn.execute(
            """INSERT INTO documents (index_id, id, source, uri, title, content_hash,
                                      chunker_version, embedding_model, user_id, metadata,
                                      sections)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (index_id, id) DO UPDATE SET
                 source = EXCLUDED.source, uri = EXCLUDED.uri, title = EXCLUDED.title,
                 content_hash = EXCLUDED.content_hash,
                 chunker_version = EXCLUDED.chunker_version,
                 embedding_model = EXCLUDED.embedding_model,
                 user_id = EXCLUDED.user_id, metadata = EXCLUDED.metadata,
                 sections = EXCLUDED.sections, updated_at = now()""",
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
                json.dumps([s.model_dump() for s in document.sections]),
            ),
        )

    async def list_document_ids(self, index: IndexInfo, source: str) -> list[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM documents WHERE index_id = %s AND source = %s ORDER BY id",
                (index.id, source),
            )
            return [r["id"] for r in await cur.fetchall()]

    async def iter_documents(
        self, index: IndexInfo, exclude_sources: set[str]
    ) -> AsyncIterator[StoredDocument]:
        """Every stored document of ``index`` outside ``exclude_sources``, in pages."""
        last = ""
        while True:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    """SELECT id, source, uri, title, content_hash, user_id, metadata, sections
                       FROM documents
                       WHERE index_id = %s AND id > %s AND NOT (source = ANY(%s))
                       ORDER BY id LIMIT 200""",
                    (index.id, last, sorted(exclude_sources)),
                )
                rows = await cur.fetchall()
            if not rows:
                return
            for r in rows:
                parsed = None
                if r["sections"] is not None:
                    parsed = ParsedDocument(
                        id=r["id"],
                        source=r["source"],
                        uri=r["uri"],
                        title=r["title"],
                        sections=[Section(**s) for s in r["sections"]],
                        metadata=r["metadata"] or {},
                        content_hash=r["content_hash"],
                    )
                yield StoredDocument(
                    id=r["id"], source=r["source"], user_id=r["user_id"], document=parsed
                )
            last = rows[-1]["id"]

    async def stats(self, index: IndexInfo) -> list[dict[str, Any]]:
        """Documents per source. Stores that can count chunks cheaply override this."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT source, count(*) AS documents, NULL::bigint AS chunks,
                          max(updated_at) AS updated_at
                   FROM documents WHERE index_id = %s GROUP BY source ORDER BY source""",
                (index.id,),
            )
            return list(await cur.fetchall())

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
