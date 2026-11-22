"""Heuristic PII redaction (on by default) and a streaming redactor.

Regexes catch common, well-formed identifiers: emails, phone numbers, payment card
numbers (Luhn-checked), IBANs, US SSNs and IPv4 addresses. They will miss names and
addresses. For stronger coverage plug Microsoft Presidio or a model-based detector in
behind the same ``Guard`` protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chatbot.core.types import GuardPayload, GuardResult


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Pattern:
    label: str
    regex: re.Pattern[str]
    check: object = None  # optional callable(str) -> bool

    def matches(self, text: str) -> list[tuple[int, int]]:
        out = []
        for m in self.regex.finditer(text):
            if self.check is None or self.check(m.group(0)):  # type: ignore[operator]
                out.append((m.start(), m.end()))
        return out


PATTERNS: list[Pattern] = [
    Pattern("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Pattern("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _luhn),
    Pattern("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")),
    Pattern("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Pattern(
        "PHONE",
        re.compile(
            r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{2,4}(?:[ .-]?\d{2,4}){2,4}\b"
        ),
        lambda s: 9 <= len(re.sub(r"\D", "", s)) <= 15,
    ),
    Pattern(
        "IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
    ),
]


def find_pii(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping spans, earliest and longest first."""
    spans: list[tuple[int, int, str]] = []
    for pattern in PATTERNS:
        spans.extend((s, e, pattern.label) for s, e in pattern.matches(text))
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, label in spans:
        if s >= last_end:
            out.append((s, e, label))
            last_end = e
    return out


def redact(text: str) -> tuple[str, list[str]]:
    spans = find_pii(text)
    if not spans:
        return text, []
    parts, pos = [], 0
    for s, e, label in spans:
        parts.append(text[pos:s])
        parts.append(f"[{label}]")
        pos = e
    parts.append(text[pos:])
    return "".join(parts), sorted({label for _, _, label in spans})


class PiiGuard:
    name = "pii_redaction"

    def __init__(self, stage: str) -> None:
        self.stage = stage

    async def check(self, payload: GuardPayload) -> GuardResult:
        text, labels = redact(payload.text)
        if not labels:
            return GuardResult(action="allow", text=payload.text)
        return GuardResult(action="modify", text=text, reasons=[f"pii:{x}" for x in labels])


class StreamRedactor:
    """Redacts PII in a token stream without ever splitting a match across two events.

    Text is held back until it is at least ``holdback`` characters behind the stream head
    and the cut point is whitespace that no PII match crosses. Anything still arriving
    (and so possibly the start of a phone number or email) stays in the buffer.
    """

    def __init__(self, holdback: int = 64) -> None:
        self.holdback = holdback
        self.pending = ""

    def _cut(self) -> int:
        limit = len(self.pending) - self.holdback
        if limit <= 0:
            return 0
        cut = max(self.pending.rfind(" ", 0, limit + 1), self.pending.rfind("\n", 0, limit + 1))
        if cut <= 0:
            return 0
        for s, e, _ in find_pii(self.pending):
            if s < cut < e:
                cut = s
        return cut

    def feed(self, text: str) -> str:
        self.pending += text
        cut = self._cut()
        if cut <= 0:
            return ""
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return redact(out)[0]

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return redact(out)[0]
