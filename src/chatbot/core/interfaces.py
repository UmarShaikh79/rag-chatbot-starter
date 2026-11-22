"""Protocols for every swappable part. Modules depend on these, never on each other."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from chatbot.core.types import (
    Calibration,
    ChatEvent,
    ChatRequest,
    ChatResponse,
    Chunk,
    Completion,
    ConversationContext,
    DeletionReport,
    Delta,
    GuardPayload,
    GuardResult,
    IndexInfo,
    IndexSummary,
    Job,
    MemoryState,
    Message,
    ParsedDocument,
    PiiSpan,
    RawDocument,
    ScoredChunk,
    StoredDocument,
    Turn,
    Usage,
    UserFact,
)


@runtime_checkable
class LLM(Protocol):
    model_id: str

    async def generate(
        self, messages: list[Message], *, schema: type[BaseModel] | None = None, **opts: Any
    ) -> Completion: ...

    def stream(self, messages: list[Message], **opts: Any) -> AsyncIterator[Delta]: ...


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    dims: int

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], Usage]: ...

    async def embed_query(self, text: str) -> tuple[list[float], Usage]: ...


@runtime_checkable
class VectorStore(Protocol):
    async def ensure_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str
    ) -> IndexInfo: ...

    async def get_index(self, alias: str) -> IndexInfo | None: ...

    async def document_state(self, index: IndexInfo, document_id: str) -> dict[str, Any] | None: ...

    async def replace_document(
        self,
        index: IndexInfo,
        document: ParsedDocument,
        chunks: list[Chunk],
        vectors: list[list[float]],
        user_id: str | None = None,
    ) -> None: ...

    async def delete_document(self, index: IndexInfo, document_id: str) -> int: ...

    async def list_document_ids(self, index: IndexInfo, source: str) -> list[str]: ...

    async def search(
        self, index: IndexInfo, vector: list[float], k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]:
        """``filters``: ``document_id`` (one ID or a list) plus metadata keys, all ANDed.
        Every store applies metadata keys as ``metadata_matches`` does."""
        ...


def metadata_matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    """The metadata filter rule every store follows (``document_id`` is handled apart).

    A scalar filter value matches the key's value, or any element when the stored value is a
    list: ``{"tags": "faq"}`` matches ``"faq"`` and ``["faq", "billing"]``. A list filter value
    requires every element: ``{"tags": ["faq", "billing"]}`` matches only a list holding both.
    """
    for key, value in filters.items():
        if key == "document_id":
            continue
        if key not in metadata:
            return False
        stored = metadata[key]
        have = stored if isinstance(stored, list) else [stored]
        for item in value if isinstance(value, list) else [value]:
            if item not in have:
                return False
    return True


@runtime_checkable
class KeywordSearch(Protocol):
    """Optional store capability: lexical (BM25) search over the same chunks (ADR 020)."""

    async def keyword_search(
        self, index: IndexInfo, query: str, k: int, filters: dict[str, Any]
    ) -> list[ScoredChunk]: ...


@runtime_checkable
class CalibrationStore(Protocol):
    """Optional store capability: calibrated thresholds per index and scorer (ADR 021)."""

    async def get_calibration(self, index: IndexInfo, scorer: str) -> Calibration | None: ...

    async def save_calibration(self, index: IndexInfo, calibration: Calibration) -> None: ...


@runtime_checkable
class IndexAdmin(Protocol):
    """Optional store capability: build a new index beside the live one, then switch the
    alias over in one step, and back again (ADR 023). Both built-in stores have it."""

    async def create_index(
        self, alias: str, embedding_model: str, dims: int, chunker_version: str, staging: str
    ) -> IndexInfo: ...

    async def mark_ready(self, index: IndexInfo, report: dict[str, Any]) -> None: ...

    async def activate(self, alias: str, staging: str, force: bool = False) -> IndexInfo: ...

    async def rollback(self, alias: str) -> IndexInfo: ...

    async def drop_index(self, name: str) -> None: ...

    async def list_indexes(self) -> list[IndexSummary]: ...

    async def resolve_any(self, alias: str) -> IndexInfo | None: ...

    def iter_documents(
        self, index: IndexInfo, exclude_sources: set[str]
    ) -> AsyncIterator[StoredDocument]: ...


@runtime_checkable
class UserDocumentEraser(Protocol):
    """Optional store capability: delete a user's vectors when they live outside Postgres,
    so ``forget_user`` leaves nothing behind (ADR 015, ADR 024)."""

    async def forget_user_documents(self, user_id: str) -> int: ...


@runtime_checkable
class Source(Protocol):
    name: str

    def iter_documents(self) -> AsyncIterator[RawDocument]: ...


@runtime_checkable
class Parser(Protocol):
    media_types: tuple[str, ...]

    def parse(self, raw: RawDocument) -> ParsedDocument: ...


@runtime_checkable
class Chunker(Protocol):
    version: str

    def split(self, doc: ParsedDocument) -> list[Chunk]: ...


@runtime_checkable
class Retriever(Protocol):
    name: str

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]: ...


@runtime_checkable
class Reranker(Protocol):
    """Re-scores retrieved hits against the query. ``signature`` names the model, because a
    reranker's scores live on their own scale and need their own calibration (ADR 011)."""

    signature: str

    async def rerank(
        self, query: str, hits: list[ScoredChunk], top_n: int
    ) -> tuple[list[ScoredChunk], Usage]: ...


@runtime_checkable
class Guard(Protocol):
    name: str
    stage: Literal["input", "context", "output"]

    async def check(self, payload: GuardPayload) -> GuardResult: ...


@runtime_checkable
class PiiDetector(Protocol):
    """Finds PII spans. Several detectors can run together; their spans are merged."""

    name: str

    def find(self, text: str) -> list[PiiSpan]: ...


@runtime_checkable
class InjectionDetector(Protocol):
    """Returns reasons (empty when the text looks safe). ``stage`` is input or context."""

    name: str

    async def detect(self, text: str, stage: str) -> list[str]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Turns, rolling summaries and long-term user facts. All text arrives redacted."""

    async def load(
        self,
        user_id: str | None,
        conversation_id: str | None,
        history_turns: int,
        facts_limit: int = 0,
    ) -> ConversationContext: ...

    async def save_turns(self, ctx: ConversationContext, turns: list[Turn]) -> None: ...

    async def memory_state(self, conversation_id: str) -> MemoryState | None: ...

    async def turns_after(self, conversation_id: str, after_id: int) -> list[Turn]: ...

    async def save_summary(self, state: MemoryState) -> None: ...

    async def add_facts(
        self, user_id: str, facts: list[str], conversation_id: str | None, max_facts: int
    ) -> int: ...

    async def list_facts(self, user_id: str, limit: int = 100) -> list[UserFact]: ...

    async def delete_fact(self, user_id: str, fact_id: int) -> bool: ...

    async def forget_user(self, user_id: str) -> DeletionReport: ...


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        dedup_key: str | None = None,
        max_attempts: int = 3,
    ) -> str: ...

    async def claim(self, kinds: list[str]) -> Job | None: ...

    async def complete(self, job_id: str, result: dict[str, Any]) -> None: ...

    async def fail(self, job_id: str, error: str) -> None: ...

    async def get(self, job_id: str) -> Job | None: ...


@runtime_checkable
class Orchestrator(Protocol):
    """Runs a chat turn. ``orchestrator: simple | langgraph | yours`` picks one from the
    registry (ADR 003, ADR 029); ``stream`` yields deltas, then exactly one terminal event."""

    async def run(self, req: ChatRequest) -> ChatResponse: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]: ...


@runtime_checkable
class UsageStore(Protocol):
    """Where usage records go and the reports over them (ADR 028)."""

    async def record(self, rec: Any) -> None: ...

    async def report(
        self,
        group_by: str = "day",
        since: Any = None,
        until: Any = None,
        user_id: str | None = None,
    ) -> Any: ...
