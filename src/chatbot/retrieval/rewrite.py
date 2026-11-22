"""Query rewriting: turn a follow-up into a standalone search query.

"And in the US?" retrieves nothing useful on its own. With the conversation so far, a cheap
model rewrites it to "Where are notes stored for US customers?". The rewrite is used only
for retrieval; the answer prompt still sees the user's own words and the history.
"""

from __future__ import annotations

import logging

from chatbot.core.interfaces import LLM
from chatbot.core.prompts import PromptRegistry
from chatbot.core.settings import QueryRewriteOptions
from chatbot.core.types import Turn, Usage

log = logging.getLogger(__name__)


class QueryRewriter:
    def __init__(self, cfg: QueryRewriteOptions, llm: LLM, prompts: PromptRegistry) -> None:
        self.cfg = cfg
        self.llm = llm
        self.prompts = prompts

    async def rewrite(self, question: str, history: list[Turn]) -> tuple[str, Usage]:
        if not self.cfg.enabled or (self.cfg.only_with_history and not history):
            return question, Usage()
        lines = [f"{'User' if t.role == 'user' else 'Assistant'}: {t.content}" for t in history]
        messages = self.prompts.get(self.cfg.prompt).render(
            question=question, history="\n".join(lines) or "(none)"
        )
        try:
            completion = await self.llm.generate(messages, purpose="rewrite_query")
        except Exception:
            log.warning("query rewrite failed; using the question as is", exc_info=True)
            return question, Usage()
        text = " ".join(completion.text.split()).strip().strip('"')
        if not text or len(text) > self.cfg.max_chars:
            return question, completion.usage
        return text, completion.usage
