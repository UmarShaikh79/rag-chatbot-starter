"""Runs guards stage by stage. The first block wins; modifications chain."""

from __future__ import annotations

from chatbot.core.interfaces import Guard
from chatbot.core.settings import GuardrailsSection
from chatbot.core.types import GuardPayload, GuardResult
from chatbot.guardrails.injection import InjectionGuard, LengthGuard
from chatbot.guardrails.pii import PiiGuard


class GuardPipeline:
    def __init__(self, guards: list[Guard]) -> None:
        self.guards = guards

    def stage(self, stage: str) -> list[Guard]:
        return [g for g in self.guards if g.stage == stage]

    async def run(self, stage: str, text: str) -> GuardResult:
        reasons: list[str] = []
        modified = False
        for guard in self.stage(stage):
            result = await guard.check(GuardPayload(text=text))
            reasons.extend(result.reasons)
            if result.action == "block":
                return GuardResult(action="block", text=text, reasons=reasons)
            if result.action == "modify":
                text, modified = result.text, True
        return GuardResult(action="modify" if modified else "allow", text=text, reasons=reasons)

    @classmethod
    def from_config(cls, cfg: GuardrailsSection) -> GuardPipeline:
        guards: list[Guard] = [LengthGuard(cfg.input.max_chars)]
        if cfg.input.injection_check:
            guards.append(InjectionGuard("input"))
        if cfg.input.pii_redaction:
            guards.append(PiiGuard("input"))
        if cfg.context.injection_check:
            guards.append(InjectionGuard("context"))
        if cfg.output.pii_redaction:
            guards.append(PiiGuard("output"))
        return cls(guards)
