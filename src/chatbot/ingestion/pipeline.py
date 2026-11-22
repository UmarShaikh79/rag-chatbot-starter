"""Ingestion: parse -> hash check -> chunk -> embed -> atomic replace (ADR 013).

A document is re-processed only when its content hash, the chunker version or the
embedding model changed. A full source sync also removes documents that disappeared
from the source, so no orphaned chunk can keep answering.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel

from chatbot.core.interfaces import Chunker, Embedder, Source, VectorStore
from chatbot.core.types import IndexInfo, RawDocument, Usage
from chatbot.ingestion.parsers import parser_for

log = logging.getLogger(__name__)

Outcome = Literal["indexed", "unchanged", "empty"]


class DocumentError(ValueError):
    """A single document could not be processed; the rest of the sync continues."""


class InlineDocument(BaseModel):
    """A document pushed through the API, e.g. a database record from your app."""

    id: str
    text: str
    title: str | None = None
    media_type: Literal["text/plain", "text/markdown", "text/html"] = "text/markdown"
    uri: str | None = None
    metadata: dict[str, Any] = {}


class IngestReport(BaseModel):
    source: str
    indexed: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []
    failed: dict[str, str] = {}
    chunks: int = 0
    usage: Usage = Usage()


class IngestionPipeline:
    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        chunker: Chunker,
        alias: str,
        batch_size: int = 64,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.chunker = chunker
        self.alias = alias
        self.batch_size = batch_size

    async def index(self) -> IndexInfo:
        return await self.store.ensure_index(
            self.alias, self.embedder.model_id, self.embedder.dims, self.chunker.version
        )

    async def ingest_document(
        self, index: IndexInfo, raw: RawDocument, report: IngestReport, force: bool = False
    ) -> Outcome:
        try:
            parsed = parser_for(raw.media_type).parse(raw)
        except ValueError:
            raise
        except Exception as exc:  # parser libraries raise their own error types
            raise DocumentError(f"could not parse {raw.id}: {exc}") from exc
        state = await self.store.document_state(index, parsed.id)
        if (
            not force
            and state is not None
            and state["content_hash"] == parsed.content_hash
            and state["chunker_version"] == self.chunker.version
            and state["embedding_model"] == self.embedder.model_id
        ):
            report.unchanged.append(parsed.id)
            return "unchanged"
        chunks = self.chunker.split(parsed)
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), self.batch_size):
            batch = [c.text for c in chunks[start : start + self.batch_size]]
            vecs, usage = await self.embedder.embed_documents(batch)
            vectors.extend(vecs)
            report.usage = report.usage.add(usage)
        index = index.model_copy(update={"chunker_version": self.chunker.version})
        await self.store.replace_document(index, parsed, chunks, vectors, user_id=raw.user_id)
        report.indexed.append(parsed.id)
        report.chunks += len(chunks)
        log.info("document indexed", extra={"document_id": parsed.id, "chunks": len(chunks)})
        return "indexed" if chunks else "empty"

    async def sync_source(self, source: Source, force: bool = False) -> IngestReport:
        index = await self.index()
        report = IngestReport(source=source.name)
        seen: set[str] = set()
        async for raw in source.iter_documents():
            seen.add(raw.id)
            try:
                await self.ingest_document(index, raw, report, force=force)
            except (ValueError, UnicodeDecodeError, OSError) as exc:
                # Bad file: record it and keep going. Provider/database errors propagate so
                # the job is retried; unchanged documents are skipped on retry.
                log.warning("document failed", extra={"document_id": raw.id, "error": str(exc)})
                report.failed[raw.id] = f"{type(exc).__name__}: {exc}"
        for doc_id in await self.store.list_document_ids(index, source.name):
            if doc_id not in seen:
                await self.store.delete_document(index, doc_id)
                report.removed.append(doc_id)
        log.info(
            "source synced",
            extra={
                "source": source.name,
                "indexed": len(report.indexed),
                "unchanged": len(report.unchanged),
                "removed": len(report.removed),
                "failed": len(report.failed),
            },
        )
        return report

    async def ingest_inline(
        self, source: str, documents: list[InlineDocument], user_id: str | None = None
    ) -> IngestReport:
        index = await self.index()
        report = IngestReport(source=source)
        for doc in documents:
            raw = RawDocument(
                id=f"{source}:{doc.id}",
                source=source,
                uri=doc.uri or f"{source}://{doc.id}",
                title=doc.title,
                media_type=doc.media_type,
                content=doc.text.encode("utf-8"),
                metadata=doc.metadata,
                user_id=user_id,
            )
            await self.ingest_document(index, raw, report)
        return report
