from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from chatbot.api.deps import components
from chatbot.core.container import Components
from chatbot.core.types import Job
from chatbot.ingestion.jobs.handlers import INGEST_DOCUMENTS, SYNC_SOURCE
from chatbot.ingestion.pipeline import InlineDocument

router = APIRouter(prefix="/v1", tags=["ingestion"])


class IngestRequest(BaseModel):
    """Either sync a configured source, or push documents (e.g. DB records) inline."""

    source: str | None = Field(
        None, description="Configured source name, or a label for inline documents"
    )
    documents: list[InlineDocument] | None = Field(None, max_length=500)
    user_id: str | None = Field(
        None, description="Owner of inline documents; forget_user removes them"
    )
    force: bool = Field(False, description="Re-embed even unchanged documents")

    @model_validator(mode="after")
    def _one_of(self) -> IngestRequest:
        if not self.documents and not self.source:
            raise ValueError("give a source name to sync, or documents to ingest")
        return self


class JobAccepted(BaseModel):
    job_id: str
    status_url: str


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def ingest(req: IngestRequest, c: Components = Depends(components)) -> JobAccepted:
    max_attempts = c.settings.ingestion.worker.max_attempts
    if req.documents:
        payload: dict[str, Any] = {
            "source": req.source or "api",
            "documents": [d.model_dump() for d in req.documents],
            "user_id": req.user_id,
        }
        job_id = await c.queue.enqueue(INGEST_DOCUMENTS, payload, max_attempts=max_attempts)
    else:
        try:
            c.settings.source(req.source or "")
        except KeyError:
            raise HTTPException(404, f"unknown source '{req.source}'") from None
        job_id = await c.queue.enqueue(
            SYNC_SOURCE,
            {"source": req.source, "force": req.force},
            dedup_key=f"{SYNC_SOURCE}:{req.source}",
            max_attempts=max_attempts,
        )
    return JobAccepted(job_id=job_id, status_url=f"/v1/jobs/{job_id}")


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, c: Components = Depends(components)) -> Job:
    job = await c.queue.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.get("/sources")
async def list_sources(c: Components = Depends(components)) -> dict[str, Any]:
    index = await c.store.get_index(c.settings.vector_store.index)
    stats = {}
    if index is not None and hasattr(c.store, "stats"):
        stats = {row["source"]: row for row in await c.store.stats(index)}
    configured = [
        {"name": s.name, "type": s.type, "path": s.path, **_counts(stats.pop(s.name, None))}
        for s in c.settings.ingestion.sources
    ]
    inline = [{"name": name, "type": "inline", **_counts(row)} for name, row in stats.items()]
    return {
        "index": None
        if index is None
        else {
            "alias": index.alias,
            "name": index.name,
            "embedding_model": index.embedding_model,
            "dims": index.dims,
            "calibrated": index.min_score is not None,
        },
        "sources": configured + inline,
    }


def _counts(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"documents": 0, "chunks": 0, "updated_at": None}
    return {"documents": row["documents"], "chunks": row["chunks"], "updated_at": row["updated_at"]}


@router.delete("/documents/{document_id:path}")
async def delete_document(document_id: str, c: Components = Depends(components)) -> dict[str, Any]:
    index = await c.store.get_index(c.settings.vector_store.index)
    deleted = await c.store.delete_document(index, document_id) if index else 0
    if not deleted:
        raise HTTPException(404, "document not found")
    return {"document_id": document_id, "deleted": True}
