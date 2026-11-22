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
from chatbot.core.interfaces import metadata_matches
from chatbot.core.types import (
    Calibration,
    Chunk,
    ConversationContext,
    DeletionReport,
    IndexInfo,
    Job,
    MemoryState,
    ParsedDocument,
    ScoredChunk,
    Turn,
    UserFact,
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
        self.calibrations: dict[tuple[int, str], Calibration] = {}

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
                if not metadata_matches(chunk.metadata, filters):
                    continue
                score = cosine(vector, vec)
                hits.append(
                    ScoredChunk(
                        chunk=chunk, score=score, retriever="dense", scores={"dense": score}
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _filtered(self, index: IndexInfo, filters: dict[str, Any]) -> list[Chunk]:
        out = []
        for (i, doc_id), items in self.chunks.items():
            if i != index.id:
                continue
            wanted = filters.get("document_id")
            if wanted is not None and doc_id not in (
                wanted if isinstance(wanted, list) else [wanted]
            ):
                continue
            out.extend(c for c, _ in items if metadata_matches(c.metadata, filters))
        return out

    async def keyword_search(
        self, index: IndexInfo, query: str, k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        from chatbot.retrieval.bm25 import bm25_rank

        # Statistics over the whole index, like the SQL version; filters only pick results.
        corpus: list[Chunk] = []
        for items in self._index_items(index):
            corpus.extend(items)
        allowed = {c.id for c in self._filtered(index, filters)}
        ranked = bm25_rank(query, [c.text for c in corpus])
        return [
            ScoredChunk(chunk=corpus[i], score=s, retriever="bm25", scores={"bm25": s})
            for i, s in ranked
            if corpus[i].id in allowed
        ][:k]

    def _index_items(self, index: IndexInfo) -> list[list[Chunk]]:
        """The chunks of each document in ``index`` (one list per document)."""
        out = []
        for (index_id, _document_id), items in self.chunks.items():
            if index_id == index.id:
                out.append([chunk for chunk, _vector in items])
        return out

    async def get_calibration(self, index: IndexInfo, scorer: str) -> Calibration | None:
        return self.calibrations.get((index.id, scorer))

    async def save_calibration(self, index: IndexInfo, calibration: Calibration) -> None:
        self.calibrations[(index.id, calibration.scorer)] = calibration.model_copy(
            update={"calibrated_at": datetime.now(UTC)}
        )

    async def list_calibrations(self, index: IndexInfo) -> list[Calibration]:
        return [c for (i, _), c in sorted(self.calibrations.items()) if i == index.id]


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.conversations: dict[str, str | None] = {}
        self.turns: list[tuple[str, str | None, Turn]] = []
        self.summaries: dict[str, MemoryState] = {}
        self.facts: dict[str, list[UserFact]] = {}
        self._turn_id = 0
        self._fact_id = 0

    async def load(
        self,
        user_id: str | None,
        conversation_id: str | None,
        history_turns: int,
        facts_limit: int = 0,
    ) -> ConversationContext:
        facts = await self.list_facts(user_id, facts_limit) if user_id and facts_limit else []
        if conversation_id is None or conversation_id not in self.conversations:
            return ConversationContext(
                conversation_id=conversation_id or uuid.uuid4().hex,
                user_id=user_id,
                facts=facts,
                is_new=True,
            )
        owner = self.conversations[conversation_id]
        if owner is not None and owner != user_id:
            raise ForbiddenError("This conversation belongs to a different user")
        history = [t for cid, _, t in self.turns if cid == conversation_id]
        history = history[-history_turns:] if history_turns else []
        state = self.summaries.get(conversation_id)
        return ConversationContext(
            conversation_id=conversation_id,
            user_id=owner,
            history=history,
            summary=state.summary if state else "",
            facts=facts,
        )

    async def save_turns(self, ctx: ConversationContext, turns: list[Turn]) -> None:
        self.conversations.setdefault(ctx.conversation_id, ctx.user_id)
        for t in turns:
            self._turn_id += 1
            stored = t.model_copy(update={"id": self._turn_id, "created_at": datetime.now(UTC)})
            self.turns.append((ctx.conversation_id, ctx.user_id, stored))

    async def memory_state(self, conversation_id: str) -> MemoryState | None:
        if conversation_id not in self.conversations:
            return None
        state = self.summaries.get(conversation_id)
        if state is None:
            state = MemoryState(
                conversation_id=conversation_id, user_id=self.conversations[conversation_id]
            )
        return state.model_copy()

    async def turns_after(self, conversation_id: str, after_id: int) -> list[Turn]:
        return [t for cid, _, t in self.turns if cid == conversation_id and (t.id or 0) > after_id]

    async def save_summary(self, state: MemoryState) -> None:
        if state.conversation_id in self.conversations:
            self.summaries[state.conversation_id] = state.model_copy()

    async def add_facts(
        self, user_id: str, facts: list[str], conversation_id: str | None, max_facts: int
    ) -> int:
        from chatbot.memory.facts import fact_key

        stored = self.facts.setdefault(user_id, [])
        added = 0
        for fact in facts:
            now = datetime.now(UTC)
            existing = next((f for f in stored if fact_key(f.fact) == fact_key(fact)), None)
            if existing is not None:
                existing.updated_at = now
                stored.remove(existing)
                stored.append(existing)
                continue
            self._fact_id += 1
            stored.append(
                UserFact(
                    id=self._fact_id,
                    fact=fact,
                    source_conversation_id=conversation_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            added += 1
        del stored[: max(len(stored) - max_facts, 0)]
        return added

    async def list_facts(self, user_id: str, limit: int = 100) -> list[UserFact]:
        return list(reversed(self.facts.get(user_id, [])))[:limit]

    async def delete_fact(self, user_id: str, fact_id: int) -> bool:
        stored = self.facts.get(user_id, [])
        for f in stored:
            if f.id == fact_id:
                stored.remove(f)
                return True
        return False

    async def forget_user(self, user_id: str) -> DeletionReport:
        before = len(self.turns)
        self.turns = [t for t in self.turns if t[1] != user_id]
        convs = [c for c, u in self.conversations.items() if u == user_id]
        for c in convs:
            del self.conversations[c]
        summaries = [c for c, s in self.summaries.items() if s.user_id == user_id or c in convs]
        for c in summaries:
            del self.summaries[c]
        facts = len(self.facts.pop(user_id, []))
        return DeletionReport(
            user_id=user_id,
            deleted={
                "user_facts": facts,
                "conversation_summaries": len(summaries),
                "turns": before - len(self.turns),
                "conversations": len(convs),
            },
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

    async def fail(self, job_id: str, error: str, retry: bool = True) -> None:
        job = self.jobs[job_id]
        job.last_error = error
        job.status = "failed" if not retry or job.attempts >= job.max_attempts else "queued"

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)
