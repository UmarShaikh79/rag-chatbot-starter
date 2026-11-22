"""PII redaction (on by default): detectors, a redactor that merges them, and a streaming
redactor.

The built-in ``regex`` detector catches common, well-formed identifiers: emails, phone
numbers, payment card numbers (Luhn-checked), IBANs, US SSNs and IPv4 addresses. It misses
names and street addresses; add the ``presidio`` detector (guardrails.pii.detectors) for
those, or register your own ``pii_detector``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from chatbot.core.interfaces import PiiDetector
from chatbot.core.registry import registry
from chatbot.core.settings import GuardrailsSection
from chatbot.core.types import GuardPayload, GuardResult, PiiSpan


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


# A placeholder this redactor wrote earlier, e.g. "[EMAIL]". Never redacted again.
_PLACEHOLDER = re.compile(r"\[[A-Z_]{2,30}\]")


def merge_spans(spans: Sequence[PiiSpan]) -> list[PiiSpan]:
    """Non-overlapping spans, earliest and longest first."""
    ordered = sorted(spans, key=lambda x: (x.start, -(x.end - x.start)))
    out: list[PiiSpan] = []
    last_end = -1
    for span in ordered:
        if span.start >= last_end:
            out.append(span)
            last_end = span.end
    return out


class RegexPiiDetector:
    name = "regex"

    def find(self, text: str) -> list[PiiSpan]:
        spans = []
        for pattern in PATTERNS:
            for start, end in pattern.matches(text):
                spans.append(PiiSpan(start=start, end=end, label=pattern.label))
        return spans


class Redactor:
    """Runs every configured detector, merges their spans and replaces them with [LABEL]."""

    def __init__(
        self, detectors: Sequence[PiiDetector] | None = None, allow: Sequence[str] = ()
    ) -> None:
        self.detectors = list(detectors) if detectors else [RegexPiiDetector()]
        self.allow = {a.strip().lower() for a in allow if a.strip()}

    def find(self, text: str) -> list[PiiSpan]:
        spans: list[PiiSpan] = []
        for detector in self.detectors:
            spans.extend(detector.find(text))
        keep = []
        for span in spans:
            value = text[span.start : span.end]
            if value.strip().lower() in self.allow or _PLACEHOLDER.fullmatch(value.strip()):
                continue
            keep.append(span)
        return merge_spans(keep)

    def redact(self, text: str) -> tuple[str, list[str]]:
        spans = self.find(text)
        if not spans:
            return text, []
        parts, pos = [], 0
        for span in spans:
            parts.append(text[pos : span.start])
            parts.append(f"[{span.label}]")
            pos = span.end
        parts.append(text[pos:])
        return "".join(parts), sorted({span.label for span in spans})

    @classmethod
    def from_config(cls, cfg: GuardrailsSection, names: Sequence[str] | None = None) -> Redactor:
        names = cfg.pii.detectors if names is None else names
        detectors = [registry.create("pii_detector", name, cfg) for name in names]
        return cls(detectors, cfg.pii.allow)

    @classmethod
    def pair_from_config(cls, cfg: GuardrailsSection) -> tuple[Redactor, Redactor]:
        """(input, output) redactors. Input: the user's messages and what is stored from them.
        Output: answers (``pii.output_detectors``; null means the same detectors). A detector
        used by both is built once, so a spaCy model loads once."""
        made: dict[str, PiiDetector] = {}

        def build(names: Sequence[str]) -> Redactor:
            for name in names:
                if name not in made:
                    made[name] = registry.create("pii_detector", name, cfg)
            return cls([made[n] for n in names], cfg.pii.allow)

        input_redactor = build(cfg.pii.detectors)
        out = cfg.pii.output_detectors
        if out is None or list(out) == list(cfg.pii.detectors):
            return input_redactor, input_redactor
        return input_redactor, build(out)


_DEFAULT = Redactor()


def find_pii(text: str) -> list[tuple[int, int, str]]:
    """Regex-only spans as (start, end, label). Kept for callers that need no config."""
    return [(s.start, s.end, s.label) for s in _DEFAULT.find(text)]


def redact(text: str) -> tuple[str, list[str]]:
    """Regex-only redaction. The orchestrator uses the configured ``Redactor`` instead."""
    return _DEFAULT.redact(text)


@registry.register("pii_detector", "regex")
def make_regex_detector(cfg: GuardrailsSection) -> RegexPiiDetector:
    return RegexPiiDetector()


@registry.register("pii_detector", "presidio")
def make_presidio_detector(cfg: GuardrailsSection) -> PiiDetector:
    from chatbot.guardrails.presidio import PresidioPiiDetector

    return PresidioPiiDetector(cfg.pii.presidio)


class PiiGuard:
    name = "pii_redaction"

    def __init__(self, stage: str, redactor: Redactor | None = None) -> None:
        self.stage = stage
        self.redactor = redactor or _DEFAULT

    async def check(self, payload: GuardPayload) -> GuardResult:
        text, labels = self.redactor.redact(payload.text)
        if not labels:
            return GuardResult(action="allow", text=payload.text)
        return GuardResult(action="modify", text=text, reasons=[f"pii:{x}" for x in labels])


class StreamRedactor:
    """Redacts PII in a token stream without ever splitting a match across two events.

    Why: a model streams text in small pieces. "Write to jane.doe@exa" then "mple.com" holds
    no complete email in either piece, so redacting each piece alone would let the address
    through. Text is held back until it is at least ``holdback`` characters behind the stream
    head and the cut point is whitespace that no PII match crosses. Anything still arriving
    (and so possibly the start of a phone number or email) stays in the buffer.
    """

    def __init__(self, holdback: int = 64, redactor: Redactor | None = None) -> None:
        self.holdback = holdback
        self.redactor = redactor or _DEFAULT
        self.pending = ""

    def _cut(self) -> int:
        limit = len(self.pending) - self.holdback
        if limit <= 0:
            return 0
        cut = max(self.pending.rfind(" ", 0, limit + 1), self.pending.rfind("\n", 0, limit + 1))
        if cut <= 0:
            return 0
        for span in self.redactor.find(self.pending):
            if span.start < cut < span.end:
                cut = span.start
        return cut

    def feed(self, text: str) -> str:
        self.pending += text
        cut = self._cut()
        if cut <= 0:
            return ""
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return self.redactor.redact(out)[0]

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return self.redactor.redact(out)[0]
