"""Protocols for every swappable part. Modules depend on these, never on each other."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from chatbot.core.types import (
    Chunk,
    Completion,
    ConversationContext,
    DeletionReport,
    Delta,
    GuardPayload,
    GuardResult,
    IndexInfo,
    Job,
    Message,
    ParsedDocument,
    RawDocument,
    ScoredChunk,
    Turn,
    Usage,
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
    ) -> list[ScoredChunk]: ...


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
class Guard(Protocol):
    name: str
    stage: Literal["input", "context", "output"]

    async def check(self, payload: GuardPayload) -> GuardResult: ...


@runtime_checkable
class MemoryStore(Protocol):
    async def load(
        self, user_id: str | None, conversation_id: str | None, history_turns: int
    ) -> ConversationContext: ...

    async def save_turns(self, ctx: ConversationContext, turns: list[Turn]) -> None: ...

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
