"""In-memory implementations of the Protocols, for fast unit tests without Postgres.

They follow the same contracts as the Postgres versions (atomic replace, dedup keys,
forget_user counts) so tests written against them stay meaningful.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from chatbot.core.errors import ForbiddenError, IndexMismatchError
from chatbot.core.types import (
    Chunk,
    ConversationContext,
    DeletionReport,
    IndexInfo,
    Job,
    ParsedDocument,
    ScoredChunk,
    Turn,
)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.indexes: dict[str, IndexInfo] = {}
        self.documents: dict[tuple[int, str], dict[str, Any]] = {}
        self.chunks: dict[tuple[int, str], list[tuple[Chunk, list[float]]]] = {}

    async def get_index(self, alias: str) -> IndexInfo | None:
        return self.indexes.get(alias)

    async def ensure_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str
    ) -> IndexInfo:
        info = self.indexes.get(alias)
        if info is None:
            info = IndexInfo(
                id=len(self.indexes) + 1,
                name=f"{alias}__{embedding_model}__{dims}",
                alias=alias,
                embedding_model=embedding_model,
                dims=dims,
                chunker_version=chunker_version,
            )
            self.indexes[alias] = info
        elif info.embedding_model != embedding_model or info.dims != dims:
            raise IndexMismatchError(f"index {alias} uses {info.embedding_model}")
        return info

    async def document_state(self, index: IndexInfo, document_id: str) -> dict[str, Any] | None:
        return self.documents.get((index.id, document_id))

    async def replace_document(
        self,
        index: IndexInfo,
        document: ParsedDocument,
        chunks: list[Chunk],
        vectors: list[list[float]],
        user_id: str | None = None,
    ) -> None:
        self.documents[(index.id, document.id)] = {
            "content_hash": document.content_hash,
            "chunker_version": index.chunker_version,
            "embedding_model": index.embedding_model,
            "source": document.source,
            "user_id": user_id,
        }
        self.chunks[(index.id, document.id)] = [
            (
                c.model_copy(
                    update={
                        "metadata": {**c.metadata, "source": document.source, "uri": document.uri}
                    }
                ),
                v,
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]

    async def delete_document(self, index: IndexInfo, document_id: str) -> int:
        self.chunks.pop((index.id, document_id), None)
        return 1 if self.documents.pop((index.id, document_id), None) else 0

    async def list_document_ids(self, index: IndexInfo, source: str) -> list[str]:
        return sorted(
            d for (i, d), row in self.documents.items() if i == index.id and row["source"] == source
        )

    async def search(
        self, index: IndexInfo, vector: list[float], k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        hits = []
        for (i, doc_id), items in self.chunks.items():
            if i != index.id:
                continue
            for chunk, vec in items:
                if "document_id" in filters:
                    wanted = filters["document_id"]
                    if doc_id not in (wanted if isinstance(wanted, list) else [wanted]):
                        continue
                meta = {k2: v for k2, v in filters.items() if k2 != "document_id"}
                if any(chunk.metadata.get(k2) != v for k2, v in meta.items()):
                    continue
                hits.append(ScoredChunk(chunk=chunk, score=cosine(vector, vec), retriever="dense"))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.conversations: dict[str, str | None] = {}
        self.turns: list[tuple[str, str | None, Turn]] = []

    async def load(
        self, user_id: str | None, conversation_id: str | None, history_turns: int
    ) -> ConversationContext:
        if conversation_id is None or conversation_id not in self.conversations:
            return ConversationContext(
                conversation_id=conversation_id or uuid.uuid4().hex, user_id=user_id, is_new=True
            )
        owner = self.conversations[conversation_id]
        if owner is not None and owner != user_id:
            raise ForbiddenError("This conversation belongs to a different user")
        history = [t for cid, _, t in self.turns if cid == conversation_id][-history_turns:]
        return ConversationContext(conversation_id=conversation_id, user_id=owner, history=history)

    async def save_turns(self, ctx: ConversationContext, turns: list[Turn]) -> None:
        self.conversations.setdefault(ctx.conversation_id, ctx.user_id)
        self.turns.extend((ctx.conversation_id, ctx.user_id, t) for t in turns)

    async def forget_user(self, user_id: str) -> DeletionReport:
        before = len(self.turns)
        self.turns = [t for t in self.turns if t[1] != user_id]
        convs = [c for c, u in self.conversations.items() if u == user_id]
        for c in convs:
            del self.conversations[c]
        return DeletionReport(
            user_id=user_id,
            deleted={"turns": before - len(self.turns), "conversations": len(convs)},
        )


class InMemoryJobQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.dedup: dict[str, str] = {}

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        dedup_key: str | None = None,
        max_attempts: int = 3,
    ) -> str:
        if dedup_key and dedup_key in self.dedup:
            existing = self.jobs[self.dedup[dedup_key]]
            if existing.status in ("queued", "running"):
                return existing.id
        now = datetime.now(UTC)
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            payload=payload,
            status="queued",
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = job
        if dedup_key:
            self.dedup[dedup_key] = job.id
        return job.id

    async def claim(self, kinds: list[str]) -> Job | None:
        for job in self.jobs.values():
            if job.status == "queued" and job.kind in kinds:
                job.status = "running"
                job.attempts += 1
                return job.model_copy()
        return None

    async def complete(self, job_id: str, result: dict[str, Any]) -> None:
        job = self.jobs[job_id]
        job.status, job.result, job.last_error = "succeeded", result, None

    async def fail(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        job.last_error = error
        job.status = "failed" if job.attempts >= job.max_attempts else "queued"

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)
