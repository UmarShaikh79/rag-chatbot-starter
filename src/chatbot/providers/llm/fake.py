"""Deterministic offline "LLM" for tests and the offline profile.

It does not generate language. For answers it quotes the source sentence that shares the
most words with the question and cites it; for clarifying questions it returns a fixed
question. When no source sentence shares a word with the question it abstains, the way a
real model is told to. It is not a relevance judge: use a real model to test abstention.
That is enough to test the whole ingestion-to-answer path without network access or keys.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from chatbot.core.registry import registry
from chatbot.core.settings import LLMProfile
from chatbot.core.types import Completion, Delta, Message, Usage
from chatbot.providers.embeddings.hashing import STOPWORDS as _STOP

_SOURCE_LINE = re.compile(r"^\[(\d+)\]\s*(?:\(.*?\)\s*)?(.*)$", re.M)
_QUESTION = re.compile(r"^Question:\s*(.*)$", re.M)
_SENTENCE = re.compile(r"[^.!?]+[.!?]?")
_WORD = re.compile(r"[a-z0-9+]+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOP}


class FakeLLM:
    def __init__(self, model_id: str = "fake-extractive", script: list[str] | None = None) -> None:
        self.model_id = model_id
        # Optional scripted replies (popped in order) let tests force any output.
        self.script = list(script or [])
        self.calls: list[list[Message]] = []

    def _reply(self, messages: list[Message], purpose: str | None) -> str:
        self.calls.append(messages)
        if self.script:
            return self.script.pop(0)
        if purpose == "clarify":
            return "Could you tell me a bit more about what you are looking for?"
        user = messages[-1].content
        question = _QUESTION.search(user)
        wanted = _words(question.group(1)) if question else set()
        best: tuple[int, int, str] | None = None  # (overlap, -order, sentence)
        order = 0
        for match in _SOURCE_LINE.finditer(user):
            body = match.group(2)
            for sentence in _SENTENCE.findall(body):
                sentence = sentence.replace("**", "").strip(" -*")
                if len(sentence) < 12:
                    continue
                order += 1
                score = len(_words(sentence) & wanted)
                if score == 0:
                    continue
                if best is None or (score, -order) > (best[0], best[1]):
                    best = (score, -order, f"{sentence[:400]} [{match.group(1)}]")
        return best[2] if best else "INSUFFICIENT_CONTEXT"

    @staticmethod
    def _usage(messages: list[Message], text: str) -> Usage:
        prompt = sum(len(m.content) for m in messages) // 4
        return Usage(prompt_tokens=prompt, completion_tokens=max(1, len(text) // 4))

    async def generate(
        self, messages: list[Message], *, schema: type[BaseModel] | None = None, **opts: Any
    ) -> Completion:
        text = self._reply(messages, opts.get("purpose"))
        return Completion(text=text, usage=self._usage(messages, text), model=self.model_id)

    async def stream(self, messages: list[Message], **opts: Any) -> AsyncIterator[Delta]:
        text = self._reply(messages, opts.get("purpose"))
        # Small, uneven pieces exercise the streaming redactor's boundaries.
        for piece in re.findall(r"\S+\s*|\s+", text):
            yield Delta(text=piece)
        yield Delta(usage=self._usage(messages, text))


@registry.register("llm", "fake")
def make_fake_llm(profile: LLMProfile) -> FakeLLM:
    return FakeLLM(profile.model)
