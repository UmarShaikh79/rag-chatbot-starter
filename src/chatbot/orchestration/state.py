"""The state of one chat turn, shared by the orchestrator steps (simple and LangGraph)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from chatbot.core.types import (
    ChatRequest,
    Citation,
    ConversationContext,
    Message,
    ScoredChunk,
    Usage,
)
from chatbot.retrieval.weak import Assessment

Route = Literal["", "retrieve", "answer", "clarify", "check_grounded", "check_output", "finish"]


class TurnState(BaseModel):
    """Everything a step may read or write. Steps set ``route`` to name what comes next."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: ChatRequest
    draft: bool = False  # stream deltas (draft_then_final) or only the terminal event
    route: Route = ""
    ctx: ConversationContext | None = None  # history, summary and facts
    question: str = ""  # the message after input guards (PII redacted)
    query: str = ""  # what was searched (a standalone rewrite of a follow-up)
    hits: list[ScoredChunk] = []
    verdict: Assessment | None = None
    sources: list[ScoredChunk] = []  # the numbered sources the answer prompt got
    messages: list[Message] = []  # the rendered answer prompt (for the prompt-leak check)
    raw: str = ""  # the model's answer before citation alignment
    answer: str = ""
    citations: list[Citation] = []
    drafted: str = ""  # text already sent as delta events
    warnings: list[str] = []
    usage: Usage = Usage()
    response: dict[str, Any] | None = None  # terminal ChatResponse fields, set by the last step
