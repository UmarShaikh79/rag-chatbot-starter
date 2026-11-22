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
    score: float  # higher is better; what the final ordering used (cosine, BM25, RRF, rerank)
    retriever: str  # which step produced ``score``: dense, bm25, hybrid, rerank
    # Every score this chunk picked up on the way, by step: {"dense": 0.61, "bm25": 7.2, ...}.
    # Weak-retrieval thresholds apply to one of these (retrieval.gate), never to a fused rank.
    scores: dict[str, float] = {}

    def gate_score(self, gate: str) -> float | None:
        if gate in self.scores:
            return self.scores[gate]
        return self.score if self.retriever == gate else None


class IndexInfo(BaseModel):
    """A physical index. The alias in config points at one of these (ADR 005)."""

    id: int
    name: str
    alias: str
    embedding_model: str
    dims: int
    chunker_version: str
    text_config: str = "english"  # Postgres text search config used for BM25 (ADR 020)
    backend: str = "pgvector"  # the vector store that holds its vectors (ADR 024)


class IndexSummary(BaseModel):
    """One physical index as ``chatbot index list`` shows it (ADR 023)."""

    name: str
    backend: str
    embedding_model: str
    dims: int
    chunker_version: str
    status: Literal["building", "ready"]
    aliases: list[str] = []  # aliases that point at it now
    previous_of: list[str] = []  # aliases that point at it as their rollback target
    documents: int = 0
    created_at: datetime | None = None
    ready_at: datetime | None = None
    build_report: dict[str, Any] = {}


class StoredDocument(BaseModel):
    """A document as an index stored it. ``document`` is None for rows written before the
    index kept parsed text (migration 0004); re-indexing cannot rebuild those on its own."""

    id: str
    source: str
    user_id: str | None = None
    document: ParsedDocument | None = None


class Calibration(BaseModel):
    """Thresholds calibrated on the golden set for one index and one retrieval pipeline.

    ``scorer`` names the score the thresholds apply to and every step that shapes it, e.g.
    ``rerank|rerank[rerank-v5.0-fast](hybrid(dense,bm25))``. Change any step and the stored
    calibration no longer matches, so the provisional config values (and a warning) apply
    until you run ``chatbot calibrate`` again (ADR 011, ADR 021).
    """

    scorer: str
    min_score: float
    min_hits: int
    metrics: dict[str, Any] = {}
    dataset: str = ""
    calibrated_at: datetime | None = None


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
    id: int | None = None  # set by the store; increases within a conversation


class UserFact(BaseModel):
    """Something worth remembering about a user across conversations. Stored redacted."""

    id: int
    fact: str
    source_conversation_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryState(BaseModel):
    """Per-conversation memory bookkeeping: the rolling summary and how far each job got."""

    conversation_id: str
    user_id: str | None = None
    summary: str = ""
    summarized_until: int = 0  # last turn id folded into the summary
    facts_until: int = 0  # last turn id that fact extraction has read


class ConversationContext(BaseModel):
    conversation_id: str
    user_id: str | None = None
    history: list[Turn] = []
    summary: str = ""  # rolling summary of turns older than the history window
    facts: list[UserFact] = []  # long-term facts for user_id, most recently seen first
    is_new: bool = False


class DeletionReport(BaseModel):
    user_id: str
    deleted: dict[str, int]


# ---------------------------------------------------------------- guardrails


class GuardPayload(BaseModel):
    text: str
    metadata: dict[str, Any] = {}  # e.g. {"system_prompt": ...} for the output stage


class PiiSpan(BaseModel):
    start: int
    end: int
    label: str  # EMAIL, PHONE, PERSON, ...
    score: float = 1.0


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
