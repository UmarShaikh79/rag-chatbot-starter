"""Groundedness: is every claim in the answer supported by the sources it was given?

One judge serves two places: the optional output check (``guardrails.output.
groundedness_check``), which turns an unsupported answer into a clarifying question, and
the eval runner's faithfulness metric.

Judges:
* ``llm`` (default): a prompt on the cheap model splits the answer into claims and marks
  each supported or not. Score = supported / claims.
* ``lexical``: each answer sentence counts as supported when most of its content words occur
  in the sources. Free and offline, and blind to paraphrase and negation; it suits the
  offline profile and tests, not production.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel

from chatbot.core.interfaces import LLM
from chatbot.core.prompts import PromptRegistry
from chatbot.core.registry import registry
from chatbot.core.settings import GroundednessOptions
from chatbot.core.types import ScoredChunk, Usage
from chatbot.orchestration.citations import format_sources
from chatbot.retrieval.bm25 import tokenize

log = logging.getLogger(__name__)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_MARKER = re.compile(r"\[\d{1,3}\]")


class Claim(BaseModel):
    claim: str
    supported: bool


class GroundednessVerdict(BaseModel):
    claims: list[Claim] = []

    @property
    def score(self) -> float:
        if not self.claims:
            return 1.0  # nothing claimed, nothing unsupported
        return sum(c.supported for c in self.claims) / len(self.claims)

    @property
    def unsupported(self) -> list[str]:
        return [c.claim for c in self.claims if not c.supported]


class Judgement(BaseModel):
    score: float
    unsupported: list[str] = []
    error: bool = False
    usage: Usage = Usage()


class LexicalJudge:
    name = "lexical"

    def __init__(self, min_overlap: float = 0.6) -> None:
        self.min_overlap = min_overlap

    async def judge(self, answer: str, sources: list[ScoredChunk]) -> Judgement:
        verdict = lexical_verdict(answer, " ".join(h.chunk.text for h in sources), self.min_overlap)
        return Judgement(score=verdict.score, unsupported=verdict.unsupported)


def lexical_verdict(answer: str, source_text: str, min_overlap: float = 0.6) -> GroundednessVerdict:
    vocab = set(tokenize(source_text))
    claims = []
    for sentence in _SENTENCE.findall(_MARKER.sub("", answer)):
        words = set(tokenize(sentence))
        if len(words) < 2:
            continue
        supported = len(words & vocab) / len(words) >= min_overlap
        claims.append(Claim(claim=sentence.strip(), supported=supported))
    return GroundednessVerdict(claims=claims)


class LlmJudge:
    name = "llm"

    def __init__(self, llm: LLM, prompts: PromptRegistry, prompt: str) -> None:
        self.llm = llm
        self.prompts = prompts
        self.prompt = prompt

    async def judge(self, answer: str, sources: list[ScoredChunk]) -> Judgement:
        messages = self.prompts.get(self.prompt).render(
            sources=format_sources(sources), answer=answer
        )
        try:
            completion = await self.llm.generate(
                messages, schema=GroundednessVerdict, purpose="judge_groundedness"
            )
            verdict = parse_verdict(completion.text)
        except Exception:
            log.warning("groundedness judge failed", exc_info=True)
            return Judgement(score=0.0, error=True)
        return Judgement(
            score=verdict.score, unsupported=verdict.unsupported, usage=completion.usage
        )


def parse_verdict(raw: str) -> GroundednessVerdict:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("judge did not return JSON")
    data: dict[str, Any] = json.loads(match.group(0))
    return GroundednessVerdict.model_validate(data)


@registry.register("groundedness_judge", "lexical")
def make_lexical_judge(
    cfg: GroundednessOptions, llms: dict[str, LLM], prompts: PromptRegistry | None
) -> LexicalJudge:
    return LexicalJudge()


@registry.register("groundedness_judge", "llm")
def make_llm_judge(
    cfg: GroundednessOptions, llms: dict[str, LLM], prompts: PromptRegistry | None
) -> LlmJudge:
    if prompts is None:
        raise ValueError("the llm groundedness judge needs the prompt registry")
    return LlmJudge(llms[cfg.model_role], prompts, cfg.prompt)
