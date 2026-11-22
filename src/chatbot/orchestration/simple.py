"""The default orchestrator: plain async Python, one readable function per step.

Flow per turn:
  input guards -> load history, summary and user facts -> rewrite the query (optional)
  -> retrieve (dense | bm25 | hybrid, HyDE and rerank optional) -> context guard
  -> weak? -> clarify | answer -> citation alignment -> groundedness check (optional)
  -> output guards (on the complete text, in every mode) -> persist (redacted)
  -> schedule memory refresh -> terminal event

Each step is a method that reads and updates a ``TurnState`` and names the next step in
``state.route``; ``_flow`` runs them in order, and the LangGraph adapter runs the same
methods as graph nodes (ADR 029), so both behave the same.

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
from chatbot.core.prompts import PromptRegistry, PromptTemplate
from chatbot.core.registry import registry
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
from chatbot.guardrails.groundedness import Judgement
from chatbot.guardrails.pii import StreamRedactor
from chatbot.guardrails.pipeline import GuardPipeline
from chatbot.memory.manager import MemoryManager, format_memory
from chatbot.observability.logging import get_trace_id
from chatbot.observability.tracing import count, root_io, span
from chatbot.observability.usage import priced, priced_mixed
from chatbot.orchestration.citations import align, format_sources
from chatbot.orchestration.state import TurnState
from chatbot.retrieval.rewrite import QueryRewriter
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
        memory_manager: MemoryManager | None = None,
        rewriter: QueryRewriter | None = None,
        judge: Any = None,
    ) -> None:
        self.settings = settings
        self.answer_llm = answer_llm
        self.cheap_llm = cheap_llm
        self.retriever = retriever
        self.memory = memory
        self.guards = guards
        self.prompts = prompts
        self.memory_manager = memory_manager
        self.rewriter = rewriter
        self.judge = judge  # groundedness judge; used when guardrails.output.groundedness_check
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
            question, _ = self.guards.redactor.redact(question)
            answer, _ = self.guards.redactor.redact(answer)
        await self.memory.save_turns(
            ctx,
            [
                Turn(role="user", content=question),
                Turn(role="assistant", content=answer, metadata={"kind": kind}),
            ],
        )
        if self.memory_manager is not None and kind != "refuse":
            await self.memory_manager.after_turn(ctx)

    async def _check_output(
        self, text: str, messages: list[Message], template: PromptTemplate | None = None
    ) -> tuple[str, list[str]]:
        """Output guards on the complete text. Returns (text, block reasons).

        ``template`` is the prompt that produced ``messages``: the prompt-leak check compares
        against its own wording, not against values filled into it (ADR 014)."""
        system = next((m.content for m in messages if m.role == "system"), "")
        metadata: dict[str, Any] = {"system_prompt": system}
        if template is not None:
            metadata["system_prompt_parts"] = template.system_literals()
        with span("output guards", type="guardrail") as s:
            result = await self.guards.run("output", text, metadata)
            s.set({"chatbot.guard.action": result.action, "chatbot.guard.reasons": result.reasons})
        if result.action == "block":
            log.warning("output blocked", extra={"reasons": result.reasons})
            count("chatbot.guard.blocks", 1, {"chatbot.guard.stage": "output"})
            return REFUSAL_TEXT, result.reasons
        return result.text, []

    async def _generate(
        self, llm: LLM, messages: list[Message], purpose: str, draft: bool, hold_prefix: str = ""
    ) -> AsyncIterator[tuple[str, str, Usage | None]]:
        """Yield ("delta", text, None) drafts, then ("done", full_text, usage)."""
        if not draft:
            completion = await llm.generate(messages, purpose=purpose)
            yield "done", completion.text, completion.usage
            return
        redactor = StreamRedactor(
            self.settings.guardrails.stream_holdback_chars, self.guards.redactor
        )
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
        """The turn as plain code. Each step reads and updates one ``TurnState`` and sets
        ``state.route`` to the step that comes next; the LangGraph adapter runs the same
        steps as graph nodes (ADR 029)."""
        st = TurnState(request=req, draft=draft)
        await self.check_input(st)  # -> retrieve | finish (blocked)
        if st.route == "retrieve":
            await self.retrieve(st)  # -> answer | clarify (weak retrieval)
        if st.route == "answer":
            async for event in self.answer(st):  # -> check_grounded | check_output | clarify
                yield event
        if st.route == "check_grounded":
            await self.check_grounded(st)  # -> check_output | clarify | finish (refused)
        if st.route == "check_output":
            await self.check_output(st)  # -> finish
        if st.route == "clarify":
            async for event in self.clarify(st):  # -> finish
                yield event
        yield self.terminal(st)

    # ------------------------------------------------------------------ the steps

    def terminal(self, st: TurnState) -> ChatEvent:
        """The one terminal event: refusal, replace (the drafts changed) or final."""
        data = st.response
        if data is None:
            raise ChatbotError("orchestrator ended without a response")
        if data["kind"] == "refuse":
            return ChatEvent(event="refusal", data=data)
        if st.draft and st.drafted.strip() != data["answer"].strip():
            return ChatEvent(event="replace", data=data)
        return ChatEvent(event="final", data=data)

    async def _finish(
        self,
        st: TurnState,
        text: str,
        kind: ResponseKind,
        warnings: list[str] | None = None,
        citations: list | None = None,
        question: str | None = None,
    ) -> None:
        assert st.ctx is not None
        await self._persist(st.ctx, st.question if question is None else question, text, kind)
        st.response = self._response(
            st.ctx, text, kind, st.usage, citations, st.warnings + (warnings or [])
        )
        st.route = "finish"
        root_io(output=text)

    async def check_input(self, st: TurnState) -> None:
        """Input guards (length, injection, PII), then load history, summary and facts."""
        cfg = self.settings
        req = st.request
        with span("input guards", type="guardrail") as s:
            checked = await self.guards.run("input", req.message)
            s.set(
                {"chatbot.guard.action": checked.action, "chatbot.guard.reasons": checked.reasons}
            )
            s.io(output=checked.text if checked.action != "block" else None)
        facts_limit = self.memory_manager.facts_in_prompt if self.memory_manager else 0
        with span("load memory") as s:
            st.ctx = await self.memory.load(
                req.user_id, req.conversation_id, cfg.memory.history_turns, facts_limit
            )
            s.set(
                {
                    "chatbot.memory.turns": len(st.ctx.history),
                    "chatbot.memory.facts": len(st.ctx.facts),
                    "chatbot.memory.summary_chars": len(st.ctx.summary),
                }
            )
        if checked.action == "block":
            log.info("input blocked", extra={"reasons": checked.reasons})
            count("chatbot.guard.blocks", 1, {"chatbot.guard.stage": "input"})
            text = TOO_LONG_TEXT if "too_long" in checked.reasons else REFUSAL_TEXT
            await self._finish(
                st, text, "refuse", warnings=checked.reasons, question="[blocked message]"
            )
            return
        st.question = checked.text
        st.warnings = [r for r in checked.reasons if r.startswith("pii:")]
        root_io(input=st.question)  # the redacted question, never the raw message
        if cfg.observability.log_message_text:
            log.info("question", extra={"text": st.question})
        st.route = "retrieve"

    async def retrieve(self, st: TurnState) -> None:
        """Rewrite follow-ups (optional), retrieve, drop chunks that look like injected
        instructions, then decide whether retrieval is strong enough to answer."""
        cfg = self.settings
        assert st.ctx is not None
        st.query = st.question
        if self.rewriter is not None:
            st.query, rewrite_usage = await self.rewriter.rewrite(st.question, st.ctx.history)
            st.usage = st.usage.add(priced(rewrite_usage, self.prices["cheap"]))
            if cfg.observability.log_message_text and st.query != st.question:
                log.info("query rewritten", extra={"text": st.query})
        scorer = getattr(self.retriever, "scorer", "dense|dense")
        with span(
            "retrieve",
            type="retriever",
            attrs={
                "chatbot.retrieval.scorer": scorer,
                "chatbot.retrieval.top_k": cfg.retrieval.top_k,
            },
        ) as s:
            s.io(input=st.query)
            hits, retrieval_usage = await self.retriever.retrieve(
                st.query, cfg.retrieval.top_k, st.request.filters
            )
            st.usage = st.usage.add(
                priced_mixed(retrieval_usage, self.prices["cheap"], self.prices["embed"])
            )
            s.set({"chatbot.retrieval.hits": len(hits)})
            s.io(output=[{"id": h.chunk.id, "score": round(h.score, 4)} for h in hits])
        # Counted before the context guard: dropping a suspicious chunk must not lower the bar.
        retrieved = len(hits)
        if self.guards.stage("context"):
            safe: list[ScoredChunk] = []
            with span("context guards", type="guardrail") as s:
                for hit in hits:
                    result = await self.guards.run("context", hit.chunk.text)
                    if result.action == "block":
                        log.warning(
                            "dropped retrieved chunk",
                            extra={"chunk_id": hit.chunk.id, "reasons": result.reasons},
                        )
                        count("chatbot.guard.blocks", 1, {"chatbot.guard.stage": "context"})
                    else:
                        safe.append(hit)
                s.set({"chatbot.guard.dropped": len(hits) - len(safe)})
            hits = safe
        st.hits = hits

        # Weak retrieval -> clarify (or refuse); never invent an answer.
        index = await self.retriever.index() if hasattr(self.retriever, "index") else None
        calibration = None
        if index is not None and hasattr(self.retriever, "calibration"):
            calibration = await self.retriever.calibration(index)
        st.verdict = verdict = assess(
            hits,
            cfg.retrieval,
            calibration,
            gate=getattr(self.retriever, "gate", None),
            warn_key=f"{index.name}:{scorer}" if index is not None else None,
            filtered=bool(st.request.filters),
            available=retrieved if retrieved < cfg.retrieval.top_k else None,
        )
        top = round(hits[0].score, 4) if hits else None
        log.info(
            "retrieval",
            extra={
                "hits": len(hits),
                "strong": len(verdict.strong),
                "min_hits": verdict.min_hits,
                "weak": verdict.weak,
                "gate": verdict.gate,
                "top_score": top,
            },
        )
        count(
            "chatbot.retrieval.requests",
            1,
            {"chatbot.retrieval.weak": verdict.weak, "chatbot.retrieval.gate": verdict.gate},
        )
        st.route = "clarify" if verdict.weak else "answer"

    async def clarify(self, st: TurnState) -> AsyncIterator[ChatEvent]:
        """Ask a clarifying question (or refuse, with answer.on_weak_retrieval: refuse)."""
        cfg = self.settings
        assert st.ctx is not None
        with span("clarify"):
            if cfg.answer.on_weak_retrieval == "refuse":
                text = WEAK_REFUSAL_TEXT
                if st.draft:
                    st.drafted += text
                    yield ChatEvent(event="delta", data={"text": text})
                await self._finish(st, text, "clarify")
                return
            topics = sorted(
                {
                    str(h.chunk.metadata.get("title"))
                    for h in st.hits
                    if h.chunk.metadata.get("title")
                }
            )
            prompt = self.prompts.get(cfg.answer.clarify_prompt)
            messages = prompt.render(
                question=st.question,
                history=self._history(st.ctx),
                memory=format_memory(st.ctx),
                topics=", ".join(topics[:5]) or "(none)",
            )
            text = ""
            async for kind, piece, u in self._generate(
                self.cheap_llm, messages, "clarify", st.draft
            ):
                if kind == "delta":
                    st.drafted += piece
                    yield ChatEvent(event="delta", data={"text": piece})
                else:
                    text = piece
                    st.usage = st.usage.add(priced(u or Usage(), self.prices["cheap"]))
            text, blocked = await self._check_output(text.strip(), messages, prompt)
            await self._finish(st, text, "refuse" if blocked else "clarify", warnings=blocked)

    async def answer(self, st: TurnState) -> AsyncIterator[ChatEvent]:
        """Answer from numbered sources, then align the [n] citations with them."""
        cfg = self.settings
        assert st.ctx is not None and st.verdict is not None
        with span("answer") as s:
            st.sources = st.verdict.usable[: cfg.answer.max_context_chunks]
            prompt = self.prompts.get(cfg.answer.prompt)
            abstain = str(prompt.output.get("abstain_token", "INSUFFICIENT_CONTEXT"))
            st.messages = prompt.render(
                question=st.question,
                sources=format_sources(st.sources),
                history=self._history(st.ctx),
                memory=format_memory(st.ctx),
            )
            async for kind, piece, u in self._generate(
                self.answer_llm, st.messages, "answer", st.draft, abstain
            ):
                if kind == "delta":
                    st.drafted += piece
                    yield ChatEvent(event="delta", data={"text": piece})
                else:
                    st.raw = piece
                    st.usage = st.usage.add(priced(u or Usage(), self.prices["answer"]))
            st.answer, st.citations = align(st.raw, st.sources)
            s.set(
                {
                    "chatbot.answer.sources": len(st.sources),
                    "chatbot.answer.citations": len(st.citations),
                }
            )

        # The model abstained, or cited nothing while citations are required: clarify.
        if abstain in st.raw or (cfg.answer.citations == "required" and not st.citations):
            log.info(
                "answer rejected",
                extra={"abstained": abstain in st.raw, "citations": len(st.citations)},
            )
            st.route = "clarify"
        elif cfg.guardrails.output.groundedness_check and self.judge is not None:
            st.route = "check_grounded"
        else:
            st.route = "check_output"

    async def check_grounded(self, st: TurnState) -> None:
        """Every claim must come from the cited sources (guardrails.output.groundedness_check)."""
        gcfg = self.settings.guardrails.groundedness
        cited = {c.marker for c in st.citations}
        with span("groundedness", type="evaluator") as s:
            judged: Judgement = await self.judge.judge(
                st.answer,
                [x for i, x in enumerate(st.sources, start=1) if i in cited] or st.sources,
            )
            s.set({"chatbot.groundedness.score": judged.score})
        st.usage = st.usage.add(priced(judged.usage, self.prices[gcfg.model_role]))
        failed = judged.score < gcfg.min_score and not (judged.error and gcfg.on_error == "allow")
        st.route = "check_output"
        if not failed:
            return
        log.warning(
            "answer not grounded",
            extra={"score": round(judged.score, 3), "unsupported": len(judged.unsupported)},
        )
        if gcfg.on_fail == "warn":
            st.warnings = [*st.warnings, "groundedness:low"]
        elif gcfg.on_fail == "refuse":
            await self._finish(st, REFUSAL_TEXT, "refuse", warnings=["groundedness"])
        else:
            st.route = "clarify"

    async def check_output(self, st: TurnState) -> None:
        """Output guards on the complete text, then persist and finish."""
        answer, blocked = await self._check_output(
            st.answer, st.messages, self.prompts.get(self.settings.answer.prompt)
        )
        if blocked:
            await self._finish(st, answer, "refuse", warnings=blocked)
            return
        await self._finish(st, answer, "answer", citations=st.citations)


@registry.register("orchestrator", "simple")
def make_simple(settings: Settings, **parts: Any) -> SimpleOrchestrator:
    return SimpleOrchestrator(settings, **parts)
