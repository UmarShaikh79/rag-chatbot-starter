"""Job kinds the worker understands. Add your own kind by adding a handler here."""

from __future__ import annotations

from typing import Any

from chatbot.core.container import Components, make_source
from chatbot.core.types import Job
from chatbot.ingestion.jobs.worker import Handler
from chatbot.ingestion.pipeline import InlineDocument
from chatbot.memory.manager import MEMORY_REFRESH

SYNC_SOURCE = "sync_source"
INGEST_DOCUMENTS = "ingest_documents"


def make_handlers(c: Components) -> dict[str, Handler]:
    async def sync_source(job: Job) -> dict[str, Any]:
        source = make_source(c.settings, job.payload["source"])
        report = await c.pipeline.sync_source(source, force=bool(job.payload.get("force")))
        return report.model_dump(mode="json")

    async def ingest_documents(job: Job) -> dict[str, Any]:
        docs = [InlineDocument.model_validate(d) for d in job.payload["documents"]]
        report = await c.pipeline.ingest_inline(
            job.payload.get("source", "api"), docs, user_id=job.payload.get("user_id")
        )
        return report.model_dump(mode="json")

    async def memory_refresh(job: Job) -> dict[str, Any]:
        return await c.memory_manager.refresh(job.payload["conversation_id"])

    handlers: dict[str, Handler] = {SYNC_SOURCE: sync_source, INGEST_DOCUMENTS: ingest_documents}
    if c.memory_manager is not None and c.memory_manager.enabled:
        handlers[MEMORY_REFRESH] = memory_refresh
    return handlers
