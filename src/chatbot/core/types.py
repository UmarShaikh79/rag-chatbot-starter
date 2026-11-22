"""Pydantic models shared by every module. Modules exchange only these types."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]
ResponseKind = Literal["answer", "clarify", "refuse"]


class Message(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    cost_usd: float | None = None

    def add(self, other: Usage) -> Usage:
        cost = None
        if self.cost_usd is not None or other.cost_usd is not None:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            embedding_tokens=self.embedding_tokens + other.embedding_tokens,
            cost_usd=cost,
        )


class Completion(BaseModel):
    text: str
    usage: Usage = Usage()
    model: str = ""


class Delta(BaseModel):
    """One streamed piece of model output. The last one may carry usage."""

    text: str = ""
    usage: Usage | None = None


# ---------------------------------------------------------------- documents


class RawDocument(BaseModel):
    """What a Source yields: bytes or text plus where it came from."""

    id: str  # stable within a source, e.g. "samples:refund-policy.md"
    source: str
    uri: str
    title: str | None = None
    media_type: str  # text/markdown, application/pdf, ...
    content: bytes
    metadata: dict[str, Any] = {}
    user_id: str | None = None  # set when a document belongs to one end user


class Section(BaseModel):
    """A structural piece of a parsed document (a heading and the text under it)."""

    heading: str | None = None
    text: str
    page: int | None = None


class ParsedDocument(BaseModel):
    id: str
    source: str
    uri: str
    title: str
    sections: list[Section]
    metadata: dict[str, Any] = {}
    content_hash: str


class Chunk(BaseModel):
    id: str
    document_id: str
    position: int
    text: str
    metadata: dict[str, Any] = {}
    content_hash: str


class ScoredChunk(BaseModel):
    chunk: Chunk
    score: float  # higher is better; cosine similarity for dense retrieval
    retriever: str


class IndexInfo(BaseModel):
    """A physical index. The alias in config points at one of these (ADR 005)."""

    id: int
    name: str
    alias: str
    embedding_model: str
    dims: int
    chunker_version: str
    min_score: float | None = None  # calibrated thresholds (ADR 011); None = uncalibrated
    min_hits: int | None = None


# ---------------------------------------------------------------- chat


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    user_id: str | None = Field(None, max_length=200)
    conversation_id: str | None = Field(None, max_length=200)
    filters: dict[str, Any] = {}
    stream: bool = False


class Citation(BaseModel):
    marker: int  # the [n] used in the answer text
    chunk_id: str
    document_id: str
    title: str
    uri: str
    score: float
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = []
    kind: ResponseKind
    conversation_id: str
    trace_id: str
    usage: Usage = Usage()
    warnings: list[str] = []


class ChatEvent(BaseModel):
    """An SSE event. Exactly one terminal event (final, replace, refusal, error) ends a stream.

    delta    -> {"text": "..."} (draft only; never treat it as the answer)
    final    -> ChatResponse fields; the drafts concatenate to response.answer
    replace  -> ChatResponse fields; a guard changed the text, swap the draft for this
    refusal  -> ChatResponse fields with kind=refuse; the request was blocked
    error    -> {"message": "...", "trace_id": "..."}
    """

    event: Literal["delta", "final", "replace", "refusal", "error"]
    data: dict[str, Any]

    @property
    def terminal(self) -> bool:
        return self.event != "delta"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime | None = None
    metadata: dict[str, Any] = {}


class ConversationContext(BaseModel):
    conversation_id: str
    user_id: str | None = None
    history: list[Turn] = []
    is_new: bool = False


class DeletionReport(BaseModel):
    user_id: str
    deleted: dict[str, int]


# ---------------------------------------------------------------- guardrails


class GuardPayload(BaseModel):
    text: str
    metadata: dict[str, Any] = {}


class GuardResult(BaseModel):
    action: Literal["allow", "modify", "block"]
    text: str
    reasons: list[str] = []


# ---------------------------------------------------------------- jobs


JobStatus = Literal["queued", "running", "succeeded", "failed"]


class Job(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
