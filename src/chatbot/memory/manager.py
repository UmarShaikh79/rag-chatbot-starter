"""Rolling summaries and long-term user facts (ADR 016).

After each turn the orchestrator calls ``after_turn``. Depending on ``memory.run_in`` the
refresh runs as a background job (default; the worker handles ``memory_refresh``) or inline
before the response returns. A refresh does two things, each switchable in config:

1. Summary: turns that have scrolled out of the history window (``memory.history_turns``)
   are folded into the conversation's rolling summary once at least
   ``summary.refresh_after_turns`` of them have piled up.
2. Facts: when the conversation has a ``user_id``, new turns are read for durable facts
   about the user ("The user is on the Team plan"). Facts are extracted from text that was
   redacted before it was stored, then checked again: a fact that still contains PII or
   trips the injection check is dropped (ADR 015).

Everything reads from and writes to the ``MemoryStore``; job payloads carry only IDs.
"""

from __future__ import annotations

import logging
from typing import Any

from chatbot.core.interfaces import LLM, JobQueue, MemoryStore
from chatbot.core.prompts import PromptRegistry
from chatbot.core.settings import MemorySection
from chatbot.core.types import ConversationContext, Turn, UserFact
from chatbot.guardrails.pipeline import GuardPipeline
from chatbot.memory.facts import ExtractedFacts, fact_key, parse_facts

log = logging.getLogger(__name__)

MEMORY_REFRESH = "memory_refresh"


def format_turns(turns: list[Turn]) -> str:
    return "\n".join(f"{'User' if t.role == 'user' else 'Assistant'}: {t.content}" for t in turns)


def format_memory(ctx: ConversationContext) -> str:
    """The ``memory`` prompt variable: rolling summary plus long-term facts."""
    parts = []
    if ctx.summary:
        parts.append(f"Summary of earlier conversation:\n{ctx.summary}")
    if ctx.facts:
        lines = "\n".join(f"- {f.fact}" for f in ctx.facts)
        parts.append(f"What the user told you in past conversations (may be outdated):\n{lines}")
    return "\n\n".join(parts) or "(none)"


class MemoryManager:
    def __init__(
        self,
        cfg: MemorySection,
        store: MemoryStore,
        llm: LLM,
        prompts: PromptRegistry,
        guards: GuardPipeline,
        queue: JobQueue | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.guards = guards
        self.queue = queue

    @property
    def enabled(self) -> bool:
        return self.cfg.summary.enabled or self.cfg.long_term.enabled

    @property
    def facts_in_prompt(self) -> int:
        return self.cfg.long_term.facts_in_prompt if self.cfg.long_term.enabled else 0

    async def after_turn(self, ctx: ConversationContext) -> None:
        """Schedule (or run) a refresh for the conversation that just got new turns."""
        if not self.enabled:
            return
        if self.cfg.run_in == "inline" or self.queue is None:
            try:
                await self.refresh(ctx.conversation_id)
            except Exception:  # memory is best effort; never fail the chat turn over it
                log.exception("memory refresh failed", extra={"conv": ctx.conversation_id})
            return
        await self.queue.enqueue(
            MEMORY_REFRESH,
            {"conversation_id": ctx.conversation_id, "user_id": ctx.user_id},
            dedup_key=f"{MEMORY_REFRESH}:{ctx.conversation_id}",
        )

    async def refresh(self, conversation_id: str) -> dict[str, Any]:
        state = await self.store.memory_state(conversation_id)
        if state is None:  # deleted (forget_user) before the job ran
            return {"skipped": "conversation not found"}
        report: dict[str, Any] = {"summarized_turns": 0, "facts_added": 0}
        cursor = min(state.summarized_until, state.facts_until)
        turns = await self.store.turns_after(conversation_id, cursor)
        before = state.model_copy()

        if self.cfg.summary.enabled:
            pending = [t for t in turns if (t.id or 0) > state.summarized_until]
            older = pending[: max(len(pending) - self.cfg.history_turns, 0)]
            if len(older) >= self.cfg.summary.refresh_after_turns:
                state.summary = await self._summarize(state.summary, older)
                state.summarized_until = older[-1].id or state.summarized_until
                report["summarized_turns"] = len(older)

        if self.cfg.long_term.enabled and state.user_id:
            fresh = [t for t in turns if (t.id or 0) > state.facts_until]
            if any(t.role == "user" for t in fresh):
                facts = await self._extract(state.user_id, fresh)
                report["facts_added"] = await self.store.add_facts(
                    state.user_id,
                    facts,
                    conversation_id,
                    self.cfg.long_term.max_facts_per_user,
                )
            if fresh:
                state.facts_until = fresh[-1].id or state.facts_until

        if state != before:
            await self.store.save_summary(state)
        return report

    # ------------------------------------------------------------------ steps

    async def _summarize(self, summary: str, turns: list[Turn]) -> str:
        opts = self.cfg.summary
        messages = self.prompts.get(opts.prompt).render(
            summary=summary or "(none)", turns=format_turns(turns), max_chars=opts.max_chars
        )
        completion = await self.llm.generate(messages, purpose="summarize")
        text, _ = self.guards.redactor.redact(completion.text.strip())
        return text[: opts.max_chars]

    async def _extract(self, user_id: str, turns: list[Turn]) -> list[str]:
        opts = self.cfg.long_term
        known: list[UserFact] = await self.store.list_facts(user_id, opts.max_facts_per_user)
        messages = self.prompts.get(opts.prompt).render(
            turns=format_turns(turns),
            known_facts="\n".join(f"- {f.fact}" for f in known) or "(none)",
            max_fact_chars=opts.max_fact_chars,
        )
        completion = await self.llm.generate(
            messages, schema=ExtractedFacts, purpose="extract_facts"
        )
        seen = {fact_key(f.fact) for f in known}
        out: list[str] = []
        for fact in parse_facts(completion.text):
            if len(fact) > opts.max_fact_chars:
                continue
            if self.guards.redactor.find(fact) or "[" in fact:
                continue  # PII or a redaction placeholder: not worth remembering (ADR 015)
            if (await self.guards.run("context", fact)).action == "block":
                log.warning("dropped a fact that looks like an injection")
                continue
            key = fact_key(fact)
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
        return out
