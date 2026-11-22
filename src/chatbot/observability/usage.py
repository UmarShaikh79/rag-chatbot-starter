"""Token and cost accounting. Cost is only reported when prices are set in models.yaml."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from chatbot.core.types import Usage


def priced(usage: Usage, prices: dict[str, float | None], *, embedding: bool = False) -> Usage:
    if embedding:
        price = prices.get("input")
        cost = None if price is None else usage.embedding_tokens * price / 1_000_000
    else:
        pin, pout = prices.get("input"), prices.get("output")
        cost = None
        if pin is not None and pout is not None:
            cost = (usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1_000_000
    return usage.model_copy(update={"cost_usd": cost})


def priced_mixed(
    usage: Usage, llm_prices: dict[str, float | None], embed_prices: dict[str, float | None]
) -> Usage:
    """Usage holding both model tokens and embedding tokens (retrieval with HyDE, say)."""
    parts = []
    if usage.prompt_tokens or usage.completion_tokens:
        parts.append(priced(usage.model_copy(update={"embedding_tokens": 0}), llm_prices))
    if usage.embedding_tokens:
        parts.append(priced(usage, embed_prices, embedding=True))
    costs = [p.cost_usd for p in parts]
    cost = None if not parts or any(c is None for c in costs) else sum(costs)  # type: ignore[arg-type]
    return usage.model_copy(update={"cost_usd": cost})


# ---------------------------------------------------------------- accounting (ADR 028)


class UsageCall(BaseModel):
    """One model or embedding call, priced with the profile's price_per_mtok."""

    kind: Literal["chat", "embedding"]
    model: str
    role: str  # answer | cheap | embed
    purpose: str  # answer, clarify, hyde, summarize, ingest, query, ...
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    cost_usd: float | None = None  # None: the profile has no price set
    duration_ms: float = 0.0
    error: str | None = None


class UsageRecord(BaseModel):
    """What one operation (a chat turn, a job, a CLI run) cost. Stored in usage_records."""

    operation: str  # chat | job:<kind> | ingest | reindex | ...
    trace_id: str
    user_id: str | None = None
    conversation_id: str | None = None
    created_at: datetime | None = None
    usage: Usage = Usage()
    calls: list[UsageCall] = []
    unpriced_calls: int = 0  # calls whose model has no price: cost_usd is a lower bound


class Ledger:
    """Collects the calls made while it is active (``metering``)."""

    def __init__(self) -> None:
        self.calls: list[UsageCall] = []
        self.user_id: str | None = None
        self.conversation_id: str | None = None

    def total(self) -> Usage:
        usage = Usage()
        for c in self.calls:
            usage = usage.add(
                Usage(
                    prompt_tokens=c.prompt_tokens,
                    completion_tokens=c.completion_tokens,
                    embedding_tokens=c.embedding_tokens,
                    cost_usd=c.cost_usd,
                )
            )
        return usage

    def record(self, operation: str, trace_id: str) -> UsageRecord:
        return UsageRecord(
            operation=operation,
            trace_id=trace_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            usage=self.total(),
            calls=list(self.calls),
            unpriced_calls=sum(1 for c in self.calls if c.cost_usd is None),
        )


_ledger: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("ledger", default=None)


@contextmanager
def metering() -> Iterator[Ledger]:
    """Every priced call inside the block lands in the yielded ledger (and in any ledger
    around it: a nested operation also counts towards the outer one)."""
    outer = _ledger.get()
    ledger = Ledger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _safe_reset(_ledger, token)
        if outer is not None:
            outer.calls.extend(ledger.calls)


def record_call(call: UsageCall) -> None:
    ledger = _ledger.get()
    if ledger is not None:
        ledger.calls.append(call)


def _safe_reset(var: contextvars.ContextVar, token: contextvars.Token) -> None:
    """Reset, unless a streamed response is closed from another context (client gone)."""
    with contextlib.suppress(ValueError):  # stream closed from another context
        var.reset(token)
