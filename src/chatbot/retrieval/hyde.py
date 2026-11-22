"""HyDE: retrieve with a hypothetical answer instead of the bare question.

A cheap model writes a short passage that *would* answer the question; its embedding sits
closer to real answer passages than a short question does. The passage is never shown to
the user or used as a fact. It costs one model call per question, helps most with short or
vague questions, and changes the dense score distribution, so it needs its own
calibration (the signature changes to ``hyde(dense)``).
"""

from __future__ import annotations

import logging
from typing import Any

from chatbot.core.interfaces import LLM, Retriever
from chatbot.core.prompts import PromptRegistry
from chatbot.core.types import IndexInfo, ScoredChunk, Usage

log = logging.getLogger(__name__)


class HydeRetriever:
    def __init__(
        self,
        inner: Retriever,
        llm: LLM,
        prompts: PromptRegistry,
        prompt: str = "hyde/v1",
        include_query: bool = True,
    ) -> None:
        self.inner = inner
        self.name = inner.name  # fusion weights and hit labels keep the inner name
        self.llm = llm
        self.prompts = prompts
        self.prompt = prompt
        self.include_query = include_query

    @property
    def signature(self) -> str:
        return f"hyde({getattr(self.inner, 'signature', self.inner.name)})"

    async def index(self) -> IndexInfo | None:
        return await self.inner.index() if hasattr(self.inner, "index") else None

    async def hypothetical(self, query: str) -> tuple[str, Usage]:
        messages = self.prompts.get(self.prompt).render(question=query)
        try:
            completion = await self.llm.generate(messages, purpose="hyde")
        except Exception:
            log.warning("hyde generation failed; retrieving with the question", exc_info=True)
            return "", Usage()
        return completion.text.strip(), completion.usage

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        passage, usage = await self.hypothetical(query)
        if not passage:
            text = query  # generation failed or came back empty: search with the question
        elif self.include_query:
            text = f"{query}\n\n{passage}"
        else:
            text = passage
        hits, inner_usage = await self.inner.retrieve(text, k, filters)
        return hits, usage.add(inner_usage)
