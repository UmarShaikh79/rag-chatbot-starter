"""Re-indexing without downtime: build a new index beside the live one, then switch (ADR 023).

Needed whenever the vectors would change: a new embedding model, a new chunker version,
another vector store, or new HNSW settings. Vectors from two models are not comparable, so
the live index cannot be updated in place: the retrievers refuse to search an index built
with a different model than the configured one (ADR 005).

``reindex`` runs with the *new* configuration while the API keeps serving the old index:

1. Create an empty index for the configured embedder, chunker and store, reachable only
   through the staging alias ``<alias>_next``. A previous unfinished build with the same
   settings is resumed; unchanged documents are skipped by content hash.
2. Fill it: every configured source is read again, and every other document in the live
   index (pushed through the API, say) is rebuilt from the parsed text the index kept.
3. Run the same pass again to catch what changed while the first pass ran. It is cheap:
   only new or changed documents are embedded.
4. Mark the index ready, unless some document could not be rebuilt (rows stored before
   migration 0004 keep no text). Then ``activate`` points the alias at it in one
   transaction, and keeps the old one for ``rollback``.

Calibrated thresholds belong to an index (ADR 021), so calibrate the staging alias before
switching: ``CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot calibrate --apply``.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from chatbot.core.errors import ChatbotError
from chatbot.core.interfaces import IndexAdmin, Source, VectorStore
from chatbot.core.types import IndexInfo
from chatbot.ingestion.pipeline import IngestionPipeline, IngestReport
from chatbot.stores.catalog import staging_alias

log = logging.getLogger(__name__)

STORED = "(stored)"  # report label for documents rebuilt from an index's stored text


class ReindexPass(BaseModel):
    sources: dict[str, IngestReport] = {}
    stored: IngestReport = IngestReport(source=STORED)
    missing: list[str] = []  # documents in the live index that could not be rebuilt


class ReindexReport(BaseModel):
    alias: str
    staging: str
    old_index: str | None
    new_index: str
    backend: str
    embedding_model: str
    dims: int
    chunker_version: str
    resumed: bool
    passes: list[ReindexPass] = []
    missing: list[str] = []
    ready: bool = False
    activated: bool = False

    def summary(self) -> dict[str, object]:
        reports = [r for p in self.passes for r in [*p.sources.values(), p.stored]]
        last = [*self.passes[-1].sources.values(), self.passes[-1].stored] if self.passes else []
        return {
            "indexed": sum(len(r.indexed) for r in reports),
            "removed": sum(len(r.removed) for r in reports),
            "changed_in_last_pass": sum(len(r.indexed) + len(r.removed) for r in last),
            "failed": {k: v for r in reports for k, v in r.failed.items()},
            "missing": len(self.missing),
        }


def _admin(store: VectorStore) -> IndexAdmin:
    if not isinstance(store, IndexAdmin):
        raise ChatbotError(
            f"{type(store).__name__} cannot build an index beside the live one; add the "
            "IndexAdmin methods (see chatbot.stores.catalog) or re-ingest into a new alias"
        )
    return store


def _same_build(info: IndexInfo, pipeline: IngestionPipeline, backend: str) -> bool:
    return (
        info.embedding_model == pipeline.embedder.model_id
        and info.dims == pipeline.embedder.dims
        and info.chunker_version == pipeline.chunker.version
        and info.backend == backend
    )


async def _fill(
    admin: IndexAdmin,
    pipeline: IngestionPipeline,
    sources: list[Source],
    old: IndexInfo | None,
    new: IndexInfo,
) -> ReindexPass:
    result = ReindexPass()
    for source in sources:
        result.sources[source.name] = await pipeline.sync_source(source, index=new)
    if old is None:
        return result
    configured = {s.name for s in sources}
    seen: set[str] = set()
    async for stored in admin.iter_documents(old, configured):
        seen.add(stored.id)
        if stored.document is None:
            result.missing.append(stored.id)
            continue
        try:
            await pipeline.ingest_parsed(new, stored.document, result.stored, stored.user_id)
        except ValueError as exc:
            result.stored.failed[stored.id] = f"{type(exc).__name__}: {exc}"
    # Deleted from the live index while we were building: delete here too.
    async for stored in admin.iter_documents(new, configured):
        if stored.id not in seen:
            await pipeline.store.delete_document(new, stored.id)
            result.stored.removed.append(stored.id)
    return result


async def reindex(
    store: VectorStore,
    pipeline: IngestionPipeline,
    sources: list[Source],
    alias: str,
    *,
    backend: str,
    restart: bool = False,
    allow_missing: bool = False,
    activate: bool = False,
    catch_up: bool = True,
) -> ReindexReport:
    """Build the index for the current configuration behind ``<alias>_next``."""
    admin = _admin(store)
    staging = staging_alias(alias)
    old = await admin.resolve_any(alias)
    existing = await admin.resolve_any(staging)
    resumed = False
    if existing is not None and not restart:
        if not _same_build(existing, pipeline, backend):
            raise ChatbotError(
                f"'{staging}' already points at {existing.name} ({existing.embedding_model}, "
                f"chunker {existing.chunker_version}, {existing.backend}), which does not "
                "match the current configuration. Pass --restart to start a new build; "
                "drop the old one afterwards with `chatbot index drop`."
            )
        new, resumed = existing, True
    else:
        new = await admin.create_index(
            alias,
            pipeline.embedder.model_id,
            pipeline.embedder.dims,
            pipeline.chunker.version,
            staging,
        )
    log.info(
        "reindex started",
        extra={"alias": alias, "new_index": new.name, "resumed": resumed},
    )
    report = ReindexReport(
        alias=alias,
        staging=staging,
        old_index=old.name if old else None,
        new_index=new.name,
        backend=new.backend,
        embedding_model=new.embedding_model,
        dims=new.dims,
        chunker_version=new.chunker_version,
        resumed=resumed,
    )
    for _ in range(2 if catch_up else 1):
        report.passes.append(await _fill(admin, pipeline, sources, old, new))
    report.missing = report.passes[-1].missing
    payload = report.model_dump(mode="json", exclude={"passes"}) | {"summary": report.summary()}
    if report.missing and not allow_missing:
        if hasattr(admin, "save_build_report"):
            await admin.save_build_report(new, payload)  # type: ignore[attr-defined]
        log.warning(
            "reindex left documents behind; not marking the index ready",
            extra={"missing": len(report.missing)},
        )
        return report
    await admin.mark_ready(new, payload)
    report.ready = True
    if activate:
        await admin.activate(alias, staging)
        report.activated = True
    log.info("reindex finished", extra={"new_index": new.name, "activated": report.activated})
    return report
