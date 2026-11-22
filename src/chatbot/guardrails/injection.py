"""Heuristic prompt-injection detection (on by default).

Catches the common, blunt attempts ("ignore previous instructions", fake system tags,
requests for the system prompt). It is a first line, not a guarantee: the answer prompt
also tells the model that sources are data. A model-based classifier can be added behind
the same ``Guard`` protocol.
"""

from __future__ import annotations

import re

from chatbot.core.types import GuardPayload, GuardResult

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|"
            r"your|the)\b.{0,30}\b(instructions?|rules|prompts?|directions|guidelines)\b",
            re.I | re.S,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"\b(reveal|show|print|repeat|output|leak|tell me)\b.{0,40}\b(system|hidden|initial)"
            r"\s+(prompt|instructions|message)",
            re.I | re.S,
        ),
    ),
    ("role_tag", re.compile(r"(<\|?(system|im_start|assistant)\|?>|\[/?(system|INST)\])", re.I)),
    (
        "role_play_jailbreak",
        re.compile(
            r"\b(you are now|act as|pretend to be)\b.{0,40}\b(DAN|jailbroken|unfiltered|"
            r"without (any )?(rules|restrictions|filters))",
            re.I | re.S,
        ),
    ),
    (
        "new_instructions",
        re.compile(r"^\s*(new|updated|system)\s+(instructions?|rules)\s*:", re.I | re.M),
    ),
]


def detect_injection(text: str) -> list[str]:
    return [name for name, rx in _RULES if rx.search(text)]


class InjectionGuard:
    name = "injection_check"

    def __init__(self, stage: str) -> None:
        self.stage = stage

    async def check(self, payload: GuardPayload) -> GuardResult:
        hits = detect_injection(payload.text)
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
