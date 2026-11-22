"""The default orchestrator: plain async Python, one readable function per step.

Flow per turn:
  input guards -> load history -> retrieve -> context guard -> weak? -> clarify | answer
  -> citation alignment -> output guards -> persist (redacted) -> terminal event

``stream`` and ``run`` share one implementation. In draft_then_final mode, delta events
carry draft text through a PII-safe rolling buffer and exactly one terminal event follows
(ADR 014). ``run`` and buffered mode emit only the terminal event.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from chatbot.core.errors import ChatbotError
from chatbot.core.interfaces import LLM, MemoryStore, Retriever
from chatbot.core.prompts import PromptRegistry
from chatbot.core.settings import Settings
from chatbot.core.types import (
    ChatEvent,
    ChatRequest,
    ChatResponse,
    ConversationContext,
    Message,
    ResponseKind,
    ScoredChunk,
    Turn,
    Usage,
)
from chatbot.guardrails.pii import StreamRedactor, redact
from chatbot.guardrails.pipeline import GuardPipeline
from chatbot.observability.logging import get_trace_id
from chatbot.observability.usage import priced
from chatbot.orchestration.citations import align, format_sources
from chatbot.retrieval.weak import assess

log = logging.getLogger(__name__)

REFUSAL_TEXT = "Sorry, I can't help with that request."
TOO_LONG_TEXT = "Your message is too long. Please shorten it and try again."
WEAK_REFUSAL_TEXT = "I couldn't find information about that in the knowledge base."


class SimpleOrchestrator:
    def __init__(
        self,
        settings: Settings,
        answer_llm: LLM,
        cheap_llm: LLM,
        retriever: Retriever,
        memory: MemoryStore,
        guards: GuardPipeline,
        prompts: PromptRegistry,
    ) -> None:
        self.settings = settings
        self.answer_llm = answer_llm
        self.cheap_llm = cheap_llm
        self.retriever = retriever
        self.memory = memory
        self.guards = guards
        self.prompts = prompts
        self.prices = {
            "answer": settings.llm_profile("answer").price_per_mtok,
            "cheap": settings.llm_profile("cheap").price_per_mtok,
            "embed": settings.embed_profile().price_per_mtok,
        }

    # ------------------------------------------------------------------ public API

    async def run(self, req: ChatRequest) -> ChatResponse:
        async for event in self._flow(req, draft=False):
            if event.event == "error":
                raise ChatbotError(event.data["message"])
            if event.terminal:
                return ChatResponse.model_validate(event.data)
        raise ChatbotError("orchestrator ended without a terminal event")

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        draft = self.settings.answer.stream_mode == "draft_then_final"
        try:
            async for event in self._flow(req, draft=draft):
                yield event
        except Exception as exc:
            log.exception("chat stream failed")
            message = str(exc) if isinstance(exc, ChatbotError) else "internal error"
            yield ChatEvent(event="error", data={"message": message, "trace_id": get_trace_id()})

    # ------------------------------------------------------------------ helpers

    def _history(self, ctx: ConversationContext) -> str:
        if not ctx.history:
            return "(none)"
        return "\n".join(
            f"{'User' if t.role == 'user' else 'Assistant'}: {t.content}" for t in ctx.history
        )

    def _response(
        self,
        ctx: ConversationContext,
        answer: str,
        kind: ResponseKind,
        usage: Usage,
        citations: list | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return ChatResponse(
            answer=answer,
            citations=citations or [],
            kind=kind,
            conversation_id=ctx.conversation_id,
            trace_id=get_trace_id(),
            usage=usage,
            warnings=warnings or [],
        ).model_dump(mode="json")

    async def _persist(
        self, ctx: ConversationContext, question: str, answer: str, kind: str
    ) -> None:
        if self.settings.memory.redact_before_persist:
            question, _ = redact(question)
            answer, _ = redact(answer)
        await self.memory.save_turns(
            ctx,
            [
                Turn(role="user", content=question),
                Turn(role="assistant", content=answer, metadata={"kind": kind}),
            ],
        )

    async def _generate(
        self, llm: LLM, messages: list[Message], purpose: str, draft: bool, hold_prefix: str = ""
    ) -> AsyncIterator[tuple[str, str, Usage | None]]:
        """Yield ("delta", text, None) drafts, then ("done", full_text, usage)."""
        if not draft:
            completion = await llm.generate(messages, purpose=purpose)
            yield "done", completion.text, completion.usage
            return
        redactor = StreamRedactor(self.settings.guardrails.stream_holdback_chars)
        full, usage, holding = "", None, bool(hold_prefix)
        async for delta in llm.stream(messages, purpose=purpose):
            if delta.usage is not None:
                usage = delta.usage
            if not delta.text:
                continue
            full += delta.text
            if holding:
                # Don't stream the abstain token: wait until the text can't be it.
                head = full.lstrip()
                if hold_prefix.startswith(head) or head.startswith(hold_prefix):
                    continue
                holding = False
                piece = redactor.feed(full)
            else:
                piece = redactor.feed(delta.text)
            if piece:
                yield "delta", piece, None
        if holding and not full.lstrip().startswith(hold_prefix):
            holding = False  # short answer that only looked like the abstain token's start
            piece = redactor.feed(full)
            if piece:
                yield "delta", piece, None
        if not holding:
            tail = redactor.flush()
            if tail:
                yield "delta", tail, None
        yield "done", full, usage

    # ------------------------------------------------------------------ the flow

    async def _flow(self, req: ChatRequest, draft: bool) -> AsyncIterator[ChatEvent]:
        cfg = self.settings
        usage = Usage()
        drafted = ""

        def terminal(data: dict[str, Any]) -> ChatEvent:
            if data["kind"] == "refuse":
                return ChatEvent(event="refusal", data=data)
            if draft and drafted.strip() != data["answer"].strip():
                return ChatEvent(event="replace", data=data)
            return ChatEvent(event="final", data=data)

        # 1. Input guards (length, injection, PII).
        checked = await self.guards.run("input", req.message)
        ctx = await self.memory.load(req.user_id, req.conversation_id, cfg.memory.history_turns)
        if checked.action == "block":
            log.info("input blocked", extra={"reasons": checked.reasons})
            text = TOO_LONG_TEXT if "too_long" in checked.reasons else REFUSAL_TEXT
            await self._persist(ctx, "[blocked message]", text, "refuse")
            yield terminal(self._response(ctx, text, "refuse", usage, warnings=checked.reasons))
            return
        question = checked.text
        warnings = [r for r in checked.reasons if r.startswith("pii:")]
        if cfg.observability.log_message_text:
            log.info("question", extra={"text": question})

        # 2. Retrieve, then drop retrieved chunks that look like injected instructions.
        hits, embed_usage = await self.retriever.retrieve(
            question, cfg.retrieval.top_k, req.filters
        )
        usage = usage.add(priced(embed_usage, self.prices["embed"], embedding=True))
        if self.guards.stage("context"):
            safe: list[ScoredChunk] = []
            for hit in hits:
                result = await self.guards.run("context", hit.chunk.text)
                if result.action == "block":
                    log.warning(
                        "dropped retrieved chunk",
                        extra={"chunk_id": hit.chunk.id, "reasons": result.reasons},
                    )
                else:
                    safe.append(hit)
            hits = safe

        # 3. Weak retrieval -> clarify (or refuse); never invent an answer.
        index = await self.retriever.index() if hasattr(self.retriever, "index") else None
        verdict = assess(hits, cfg.retrieval, index)
        log.info(
            "retrieval",
            extra={
                "hits": len(hits),
                "strong": len(verdict.strong),
                "weak": verdict.weak,
                "top_score": round(hits[0].score, 4) if hits else None,
            },
        )

        async def clarify() -> AsyncIterator[ChatEvent]:
            nonlocal usage, drafted
            if cfg.answer.on_weak_retrieval == "refuse":
                text = WEAK_REFUSAL_TEXT
                if draft:
                    drafted += text
                    yield ChatEvent(event="delta", data={"text": text})
                await self._persist(ctx, question, text, "clarify")
                yield terminal(self._response(ctx, text, "clarify", usage, warnings=warnings))
                return
            topics = sorted(
                {str(h.chunk.metadata.get("title")) for h in hits if h.chunk.metadata.get("title")}
            )
            prompt = self.prompts.get(cfg.answer.clarify_prompt)
            messages = prompt.render(
                question=question,
                history=self._history(ctx),
                topics=", ".join(topics[:5]) or "(none)",
            )
            text = ""
            async for kind, piece, u in self._generate(self.cheap_llm, messages, "clarify", draft):
                if kind == "delta":
                    drafted += piece
                    yield ChatEvent(event="delta", data={"text": piece})
                else:
                    text = piece
                    usage = usage.add(priced(u or Usage(), self.prices["cheap"]))
            text = (await self.guards.run("output", text)).text.strip()
            await self._persist(ctx, question, text, "clarify")
            yield terminal(self._response(ctx, text, "clarify", usage, warnings=warnings))

        if verdict.weak:
            async for event in clarify():
                yield event
            return

        # 4. Answer from numbered sources.
        sources = verdict.strong[: cfg.answer.max_context_chunks]
        prompt = self.prompts.get(cfg.answer.prompt)
        abstain = str(prompt.output.get("abstain_token", "INSUFFICIENT_CONTEXT"))
        messages = prompt.render(
            question=question, sources=format_sources(sources), history=self._history(ctx)
        )
        raw = ""
        async for kind, piece, u in self._generate(
            self.answer_llm, messages, "answer", draft, abstain
        ):
            if kind == "delta":
                drafted += piece
                yield ChatEvent(event="delta", data={"text": piece})
            else:
                raw = piece
                usage = usage.add(priced(u or Usage(), self.prices["answer"]))

        # 5. The model abstained, or cited nothing while citations are required: clarify.
        answer, citations = align(raw, sources)
        if abstain in raw or (cfg.answer.citations == "required" and not citations):
            log.info(
                "answer rejected", extra={"abstained": abstain in raw, "citations": len(citations)}
            )
            async for event in clarify():
                yield event
            return

        # 6. Output guards on the complete text, then persist and finish.
        answer = (await self.guards.run("output", answer)).text
        await self._persist(ctx, question, answer, "answer")
        yield terminal(self._response(ctx, answer, "answer", usage, citations, warnings))
