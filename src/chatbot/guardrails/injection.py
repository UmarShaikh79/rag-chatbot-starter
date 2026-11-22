"""Prompt-injection detection (on by default) and output checks.

Two detectors ship, chosen with ``guardrails.injection.detectors``:

- ``heuristic`` (default): rules for the common attempts ("ignore previous instructions",
  fake role tags, requests for the system prompt, data exfiltration through links), run on
  the text after Unicode normalisation, with zero-width characters removed, leetspeak and
  spaced-out letters undone, and short base64 payloads decoded. Free and fast; a first line,
  not a guarantee.
- ``llm``: asks the cheap model to classify the text with the ``guard_injection`` prompt.
  Catches paraphrases the rules miss, costs one call per checked text, so by default it
  only runs on user input.

The answer prompt also tells the model that sources are data. Register your own
``injection_detector`` (for example a hosted classifier) to add more.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import unicodedata
from typing import Any

from pydantic import BaseModel

from chatbot.core.interfaces import LLM, InjectionDetector
from chatbot.core.prompts import PromptRegistry
from chatbot.core.registry import registry
from chatbot.core.settings import GuardrailsSection
from chatbot.core.types import GuardPayload, GuardResult

log = logging.getLogger(__name__)

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,40}\b(previous|prior|above|"
            r"earlier|all|any|your|the|these|those)\b.{0,30}\b(instructions?|rules|prompts?|"
            r"directions|guidelines|guardrails|polic(y|ies))\b",
            re.I | re.S,
        ),
    ),
    (
        "override_instructions",
        # Spanish, French, German, Portuguese, Italian
        re.compile(
            r"\b(ignora|olvida|ignore[sz]?|oublie[sz]?|ignorier(e|en)?|vergiss|esque[cç]a)\b"
            r".{0,40}\b(instrucciones|instructions|consignes|anweisungen|regeln|instru[cç][õo]es|"
            r"istruzioni|reglas|r[eè]gles)\b",
            re.I | re.S,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"\b(reveal|show|print|repeat|output|leak|tell me|display|dump|give me)\b.{0,40}"
            r"\b(system|hidden|initial|original|developer)\s+(prompt|instructions|message)",
            re.I | re.S,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"\bwhat\s+(is|are|were)\s+your\s+(system\s+)?(prompt|instructions|rules)\b", re.I
        ),
    ),
    (
        "role_tag",
        re.compile(
            r"(<\|?(system|im_start|im_end|assistant|endoftext)\|?>|\[/?(system|INST)\]|"
            r"<<\s*/?SYS\s*>>|</?(system|instructions?)>)",
            re.I,
        ),
    ),
    (
        "role_header",
        re.compile(r"^\s*#{2,}\s*(system|instruction)s?(\s+prompt)?\s*:?\s*$", re.I | re.M),
    ),
    (
        "role_play_jailbreak",
        re.compile(
            r"\b(you are now|act as|pretend to be|roleplay as|from now on you are)\b.{0,40}"
            r"\b(DAN|jailbroken|unfiltered|uncensored|evil|without (any )?(rules|restrictions|"
            r"filters|limits))",
            re.I | re.S,
        ),
    ),
    (
        "developer_mode",
        re.compile(
            r"\b(enable|enter|activate|switch to)\b.{0,20}\b(developer|dev|god|jailbreak|debug)"
            r"\s+mode\b",
            re.I | re.S,
        ),
    ),
    (
        "new_instructions",
        re.compile(r"^\s*(new|updated|system|admin)\s+(instructions?|rules)\s*:", re.I | re.M),
    ),
    (
        "exfiltration",
        # Markdown images whose URL carries a query string are a classic way to leak data.
        re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]*\?[^)\s]*=", re.I),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|forward|upload|exfiltrate|transmit)\b.{0,60}\b(conversation|chat "
            r"history|system prompt|api key|password|credentials|secrets?)\b.{0,40}"
            r"(https?://|\bto\s+\S+@)",
            re.I | re.S,
        ),
    ),
]

_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_LEET = str.maketrans("013457@$", "oieastas")
# Letters spelled out one by one: "i g n o r e" or "i.g.n.o.r.e"
_SPACED = re.compile(r"\b(?:[A-Za-z][ ._\-]){3,}[A-Za-z]\b")
_B64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def _variants(text: str) -> list[str]:
    """The text as written plus de-obfuscated versions worth checking."""
    base = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    out = [base]
    despaced = _SPACED.sub(lambda m: re.sub(r"[ ._\-]", "", m.group(0)), base)
    if despaced != base:
        out.append(despaced)
    leet = base.translate(_LEET)
    if leet != base:
        out.append(leet)
    for blob in _B64.findall(base)[:5]:
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            plain = decoded.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if sum(ch.isprintable() for ch in plain) >= 0.9 * len(plain):
            out.append(plain)
    return out


def detect_injection(text: str) -> list[str]:
    """Names of the heuristic rules the text trips, in rule order, without duplicates."""
    found: list[str] = []
    for variant in _variants(text):
        for name, rx in _RULES:
            if name not in found and rx.search(variant):
                found.append(name)
    return found


class HeuristicInjectionDetector:
    name = "heuristic"

    async def detect(self, text: str, stage: str) -> list[str]:
        return detect_injection(text)


class InjectionVerdict(BaseModel):
    injection: bool
    reason: str = ""


class LlmInjectionDetector:
    """Classifies text with a prompt on the cheap model. Off by default (costs a call)."""

    name = "llm"

    def __init__(
        self,
        llm: LLM,
        prompts: PromptRegistry,
        prompt: str,
        stages: list[str],
        on_error: str = "allow",
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.prompt = prompt
        self.stages = set(stages)
        self.on_error = on_error

    async def detect(self, text: str, stage: str) -> list[str]:
        if stage not in self.stages:
            return []
        try:
            messages = self.prompts.get(self.prompt).render(text=text, stage=stage)
            completion = await self.llm.generate(
                messages, schema=InjectionVerdict, purpose="guard_injection"
            )
            verdict = parse_verdict(completion.text)
        except Exception:
            log.warning("injection classifier failed", exc_info=True)
            return ["classifier_error"] if self.on_error == "block" else []
        return ["llm"] if verdict.injection else []


def parse_verdict(raw: str) -> InjectionVerdict:
    """Accepts the JSON object alone or wrapped in prose / code fences."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("classifier did not return JSON")
    data: dict[str, Any] = json.loads(match.group(0))
    return InjectionVerdict.model_validate(data)


class InjectionGuard:
    name = "injection_check"

    def __init__(self, stage: str, detectors: list[InjectionDetector] | None = None) -> None:
        self.stage = stage
        self.detectors = detectors if detectors is not None else [HeuristicInjectionDetector()]

    async def check(self, payload: GuardPayload) -> GuardResult:
        hits: list[str] = []
        for detector in self.detectors:
            hits.extend(await detector.detect(payload.text, self.stage))
            if hits:
                break  # cheap detectors run first; skip the costly ones once one fires
        if hits:
            return GuardResult(
                action="block", text=payload.text, reasons=[f"injection:{h}" for h in hits]
            )
        return GuardResult(action="allow", text=payload.text)


class LengthGuard:
    name = "max_chars"
    stage = "input"

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars

    async def check(self, payload: GuardPayload) -> GuardResult:
        if len(payload.text) > self.max_chars:
            return GuardResult(action="block", text=payload.text, reasons=["too_long"])
        return GuardResult(action="allow", text=payload.text)


_WORDS = re.compile(r"[a-z0-9']+")


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORDS.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


class PromptLeakGuard:
    """Output check: refuses an answer that repeats a run of the system prompt verbatim.

    The orchestrator passes the rendered system prompt as ``metadata["system_prompt"]``.
    ``window`` consecutive words in common counts as a leak.
    """

    name = "prompt_leak_check"
    stage = "output"

    def __init__(self, window: int = 10) -> None:
        self.window = window

    async def check(self, payload: GuardPayload) -> GuardResult:
        system = str(payload.metadata.get("system_prompt") or "")
        if system and _shingles(payload.text, self.window) & _shingles(system, self.window):
            return GuardResult(action="block", text=payload.text, reasons=["prompt_leak"])
        return GuardResult(action="allow", text=payload.text)


@registry.register("injection_detector", "heuristic")
def make_heuristic(cfg: GuardrailsSection, llms: dict[str, LLM], prompts: PromptRegistry):
    return HeuristicInjectionDetector()


@registry.register("injection_detector", "llm")
def make_llm_detector(cfg: GuardrailsSection, llms: dict[str, LLM], prompts: PromptRegistry):
    opts = cfg.injection.llm
    if opts.model_role not in llms:
        raise RuntimeError("the llm injection detector needs the answer and cheap models")
    return LlmInjectionDetector(
        llms[opts.model_role], prompts, opts.prompt, list(opts.stages), opts.on_error
    )
