"""Runs guards stage by stage. The first block wins; modifications chain.

Stages: ``input`` (the user's message), ``context`` (each retrieved chunk, and long-term
facts before they are stored) and ``output`` (the complete answer, also in streaming mode).
"""

from __future__ import annotations

from typing import Any

from chatbot.core.interfaces import LLM, Guard, InjectionDetector
from chatbot.core.prompts import PromptRegistry
from chatbot.core.registry import registry
from chatbot.core.settings import GuardrailsSection
from chatbot.core.types import GuardPayload, GuardResult
from chatbot.guardrails.injection import InjectionGuard, LengthGuard, PromptLeakGuard
from chatbot.guardrails.pii import PiiGuard, Redactor


class GuardPipeline:
    def __init__(
        self,
        guards: list[Guard],
        redactor: Redactor | None = None,
        output_redactor: Redactor | None = None,
    ) -> None:
        self.guards = guards
        # PII redactors. ``redactor``: user messages and what is stored from them (questions,
        # summaries, facts). ``output_redactor``: answers, in the output guard, the streamed
        # drafts and the stored answer, so all three agree (``guardrails.pii.output_detectors``).
        self.redactor = redactor or Redactor()
        self.output_redactor = output_redactor or self.redactor

    def stage(self, stage: str) -> list[Guard]:
        return [g for g in self.guards if g.stage == stage]

    async def run(
        self, stage: str, text: str, metadata: dict[str, Any] | None = None
    ) -> GuardResult:
        reasons: list[str] = []
        modified = False
        for guard in self.stage(stage):
            result = await guard.check(GuardPayload(text=text, metadata=metadata or {}))
            reasons.extend(result.reasons)
            if result.action == "block":
                return GuardResult(action="block", text=text, reasons=reasons)
            if result.action == "modify":
                text, modified = result.text, True
        return GuardResult(action="modify" if modified else "allow", text=text, reasons=reasons)

    @classmethod
    def from_config(
        cls,
        cfg: GuardrailsSection,
        llms: dict[str, LLM] | None = None,
        prompts: PromptRegistry | None = None,
    ) -> GuardPipeline:
        """Build the configured guards. ``llms`` / ``prompts`` are needed only by the
        model-based injection detector."""
        import chatbot.guardrails.injection  # noqa: F401  (registers the detectors)

        redactor, output_redactor = Redactor.pair_from_config(cfg)
        detectors: list[InjectionDetector] = []
        if cfg.input.injection_check or cfg.context.injection_check:
            detectors = [
                registry.create("injection_detector", name, cfg, llms or {}, prompts)
                for name in cfg.injection.detectors
            ]
        guards: list[Guard] = [LengthGuard(cfg.input.max_chars)]
        if cfg.input.injection_check:
            guards.append(InjectionGuard("input", detectors))
        if cfg.input.pii_redaction:
            guards.append(PiiGuard("input", redactor))
        if cfg.context.injection_check:
            guards.append(InjectionGuard("context", detectors))
        if cfg.output.prompt_leak_check:
            guards.append(PromptLeakGuard())
        if cfg.output.pii_redaction:
            guards.append(PiiGuard("output", output_redactor))
        return cls(guards, redactor, output_redactor)
