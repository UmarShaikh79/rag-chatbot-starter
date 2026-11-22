"""Wrappers that add tracing, metrics and cost accounting to any LLM or embedder.

The container wraps every model it builds, so providers stay plain adapters with no
telemetry code. A wrapper keeps the wrapped object's interface (``model_id``, ``dims``,
``generate``, ``stream``, ``embed_*``) and passes any other attribute through.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from chatbot.core.types import (
    ChatEvent,
    ChatRequest,
    ChatResponse,
    Completion,
    Delta,
    Message,
    Usage,
)
from chatbot.observability.logging import (
    get_trace_id,
    new_trace_id,
    peek_trace_id,
    reset_trace_id,
    set_trace_id,
)
from chatbot.observability.tracing import (
    SESSION_ATTR,
    SpanHandle,
    count,
    observe,
    span,
    trace_attributes,
)
from chatbot.observability.usage import Ledger, UsageCall, metering, priced, record_call


def _messages(messages: list[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


class _Wrapper:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @property
    def model_id(self) -> str:
        return self.inner.model_id


def unwrap(obj: Any) -> Any:
    while isinstance(obj, _Wrapper):
        obj = obj.inner
    return obj


class MeteredLLM(_Wrapper):
    """A generation span per call (``chat <model>``), token and cost metrics, and a
    UsageCall in the current ledger."""

    def __init__(
        self, inner: Any, role: str, provider: str, prices: dict[str, float | None]
    ) -> None:
        super().__init__(inner)
        self.role = role
        self.provider = provider
        self.prices = prices

    def _attrs(self, purpose: str, opts: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in opts.items() if k != "purpose"}
        return {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self.provider,
            "gen_ai.request.model": self.model_id,
            "chatbot.model_role": self.role,
            "chatbot.purpose": purpose,
            "langfuse.observation.model.parameters": params or None,
        }

    def _finish(
        self, s: SpanHandle, purpose: str, usage: Usage, started: float, error: str | None
    ) -> Usage:
        seconds = time.perf_counter() - started
        priced_usage = priced(usage, self.prices)
        s.usage(usage.prompt_tokens, usage.completion_tokens, priced_usage.cost_usd)
        metric = {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.model_id,
            "chatbot.purpose": purpose,
        }
        observe("gen_ai.client.operation.duration", seconds, {**metric, "error.type": error})
        for token_type, n in (
            ("input", usage.prompt_tokens),
            ("output", usage.completion_tokens),
        ):
            if n:
                observe("gen_ai.client.token.usage", n, {**metric, "gen_ai.token.type": token_type})
        if priced_usage.cost_usd:
            count("chatbot.cost", priced_usage.cost_usd, metric)
        record_call(
            UsageCall(
                kind="chat",
                model=self.model_id,
                role=self.role,
                purpose=purpose,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=priced_usage.cost_usd,
                duration_ms=round(seconds * 1000, 1),
                error=error,
            )
        )
        return priced_usage

    async def generate(
        self, messages: list[Message], *, schema: type[BaseModel] | None = None, **opts: Any
    ) -> Completion:
        purpose = str(opts.get("purpose") or "generate")
        with span(
            f"chat {self.model_id}", type="generation", attrs=self._attrs(purpose, opts)
        ) as s:
            s.io(input=_messages(messages))
            started = time.perf_counter()
            try:
                completion = await self.inner.generate(messages, schema=schema, **opts)
            except Exception as exc:
                self._finish(s, purpose, Usage(), started, type(exc).__name__)
                raise
            self._finish(s, purpose, completion.usage, started, None)
            s.io(output=completion.text)
            return completion

    async def stream(self, messages: list[Message], **opts: Any) -> AsyncIterator[Delta]:
        purpose = str(opts.get("purpose") or "generate")
        with span(
            f"chat {self.model_id}", type="generation", attrs=self._attrs(purpose, opts)
        ) as s:
            s.io(input=_messages(messages))
            started = time.perf_counter()
            text, usage, first = "", Usage(), True
            try:
                async for delta in self.inner.stream(messages, **opts):
                    if first and delta.text:
                        first = False
                        s.set(
                            {
                                "langfuse.observation.completion_start_time": datetime.now(
                                    UTC
                                ).isoformat()
                            }
                        )
                    text += delta.text
                    if delta.usage is not None:
                        usage = delta.usage
                    yield delta
            except Exception as exc:
                self._finish(s, purpose, usage, started, type(exc).__name__)
                raise
            self._finish(s, purpose, usage, started, None)
            s.io(output=text)


class MeteredEmbedder(_Wrapper):
    """An embedding span per call, token metrics and a UsageCall in the current ledger."""

    def __init__(self, inner: Any, provider: str, prices: dict[str, float | None]) -> None:
        super().__init__(inner)
        self.provider = provider
        self.prices = prices

    @property
    def dims(self) -> int:
        return self.inner.dims

    async def _call(self, method: str, arg: Any, n: int, purpose: str) -> Any:
        attrs = {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.provider.name": self.provider,
            "gen_ai.request.model": self.model_id,
            "chatbot.purpose": purpose,
            "chatbot.embedding.inputs": n,
        }
        with span(f"embeddings {self.model_id}", type="embedding", attrs=attrs) as s:
            started = time.perf_counter()
            error = None
            usage = Usage()
            try:
                result, usage = await getattr(self.inner, method)(arg)
                return result, usage
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                seconds = time.perf_counter() - started
                cost = priced(usage, self.prices, embedding=True).cost_usd
                s.usage(usage.embedding_tokens, 0, cost)
                metric = {
                    "gen_ai.operation.name": "embeddings",
                    "gen_ai.request.model": self.model_id,
                    "chatbot.purpose": purpose,
                }
                observe(
                    "gen_ai.client.operation.duration", seconds, {**metric, "error.type": error}
                )
                if usage.embedding_tokens:
                    observe(
                        "gen_ai.client.token.usage",
                        usage.embedding_tokens,
                        {**metric, "gen_ai.token.type": "input"},
                    )
                if cost:
                    count("chatbot.cost", cost, metric)
                record_call(
                    UsageCall(
                        kind="embedding",
                        model=self.model_id,
                        role="embed",
                        purpose=purpose,
                        embedding_tokens=usage.embedding_tokens,
                        cost_usd=cost,
                        duration_ms=round(seconds * 1000, 1),
                        error=error,
                    )
                )

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()
        return await self._call("embed_documents", texts, len(texts), "documents")

    async def embed_query(self, text: str) -> tuple[list[float], Usage]:
        return await self._call("embed_query", text, 1, "query")


class MeteredOrchestrator(_Wrapper):
    """One root span per chat turn (``chat``), turn metrics, and a usage record per turn.

    Wraps whichever orchestrator is configured (simple, langgraph or yours), so tracing and
    accounting behave the same for all of them. Other attributes pass through.
    """

    def __init__(
        self, inner: Any, name: str, usage_store: Any | None = None, operation: str = "chat"
    ) -> None:
        super().__init__(inner)
        self.name = name
        self.usage_store = usage_store
        self.operation = operation

    def _attrs(self, req: ChatRequest, stream: bool) -> dict[str, Any]:
        return {
            "chatbot.orchestrator": self.name,
            "chatbot.stream": stream,
            "chatbot.conversation.new": req.conversation_id is None,
        }

    async def _close(
        self,
        s: SpanHandle,
        ledger: Ledger,
        started: float,
        kind: str,
        stream: bool,
        data: dict[str, Any] | None,
    ) -> None:
        seconds = time.perf_counter() - started
        attrs = {"chatbot.kind": kind, "chatbot.stream": stream, "chatbot.orchestrator": self.name}
        count("chatbot.chat.turns", 1, attrs)
        observe("chatbot.chat.duration", seconds, attrs)
        s.set({"chatbot.kind": kind})
        if data and data.get("conversation_id"):
            ledger.conversation_id = data["conversation_id"]
            s.set({SESSION_ATTR: data["conversation_id"]})
        if data and data.get("usage"):
            u = data["usage"]
            s.set(
                {
                    "chatbot.usage.prompt_tokens": u.get("prompt_tokens"),
                    "chatbot.usage.completion_tokens": u.get("completion_tokens"),
                    "chatbot.usage.embedding_tokens": u.get("embedding_tokens"),
                    "chatbot.usage.cost_usd": u.get("cost_usd"),
                }
            )
        if self.usage_store is not None:
            trace_id = (data or {}).get("trace_id") or get_trace_id()
            await self.usage_store.record(ledger.record(self.operation, trace_id))

    async def run(self, req: ChatRequest) -> ChatResponse:
        with (
            _own_trace_id(),
            trace_attributes(req.user_id, req.conversation_id),
            metering() as ledger,
            span(self.operation, type="chain", root=True, attrs=self._attrs(req, False)) as s,
        ):
            _adopt_trace_id(s)
            ledger.user_id = req.user_id
            started = time.perf_counter()
            try:
                resp = await self.inner.run(req)
            except Exception:
                await self._close(s, ledger, started, "error", False, None)
                raise
            await self._close(s, ledger, started, resp.kind, False, resp.model_dump(mode="json"))
            return resp

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        with (
            _own_trace_id(),
            trace_attributes(req.user_id, req.conversation_id),
            metering() as ledger,
            span(self.operation, type="chain", root=True, attrs=self._attrs(req, True)) as s,
        ):
            _adopt_trace_id(s)
            ledger.user_id = req.user_id
            started = time.perf_counter()
            kind, data = "error", None
            try:
                async for event in self.inner.stream(req):
                    if event.terminal:
                        data = event.data
                        kind = str(data.get("kind") or event.event)
                        if event.event == "error":
                            kind = "error"
                            s.fail(str(data.get("message", "error")))
                        # Account before the client sees the end of the stream.
                        await self._close(s, ledger, started, kind, True, data)
                    yield event
            finally:
                if data is None:  # the stream ended early (client gone, cancelled)
                    await self._close(s, ledger, started, "cancelled", True, None)


@contextmanager
def _own_trace_id() -> Iterator[None]:
    """Outside a request (CLI, evals) a turn gets its own trace ID; inside one, it keeps the
    request's, so the response, the logs and the trace share one ID."""
    if peek_trace_id() is not None:
        yield
        return
    token = set_trace_id(new_trace_id())
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):  # stream closed from another context
            reset_trace_id(token)


def _adopt_trace_id(s: SpanHandle) -> None:
    """Use the OpenTelemetry trace ID as the chatbot trace ID when this span starts a trace."""
    if s.trace_id and peek_trace_id() != s.trace_id:
        set_trace_id(s.trace_id)


@asynccontextmanager
async def metered_operation(
    operation: str,
    usage_store: Any | None = None,
    user_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> AsyncIterator[SpanHandle]:
    """A root span and a usage record around any unit of work that is not a chat turn:
    a background job, ``chatbot ingest --inline``, a re-index. The record is written even
    when the work fails, since its model calls were made (and billed) anyway."""
    with (
        _own_trace_id(),
        trace_attributes(user_id),
        metering() as ledger,
        span(operation, type="span", root=True, attrs=attrs) as s,
    ):
        _adopt_trace_id(s)
        ledger.user_id = user_id
        try:
            yield s
        finally:
            if usage_store is not None:
                await usage_store.record(ledger.record(operation, get_trace_id()))
