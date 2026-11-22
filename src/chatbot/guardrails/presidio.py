"""Microsoft Presidio PII detector (optional): finds names, places and more with spaCy NER.

Install with ``uv sync --extra presidio`` and download the spaCy model named in
``guardrails.pii.presidio.spacy_model`` (``uv run python -m spacy download en_core_web_sm``).
Then list it in config::

    guardrails:
      pii:
        detectors: [regex, presidio]

Presidio entity names are mapped to the labels the regex detector uses where they overlap,
so redacted text reads the same whichever detector found a value.
"""

from __future__ import annotations

from typing import Any

from chatbot.core.settings import PresidioOptions
from chatbot.core.types import PiiSpan

LABELS = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "IBAN",
    "US_SSN": "SSN",
    "IP_ADDRESS": "IP",
}


class PresidioPiiDetector:
    name = "presidio"

    def __init__(self, cfg: PresidioOptions, analyzer: Any | None = None) -> None:
        self.cfg = cfg
        self.analyzer = analyzer if analyzer is not None else self._build(cfg)

    @staticmethod
    def _build(cfg: PresidioOptions) -> Any:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RuntimeError(
                "guardrails.pii.detectors includes 'presidio' but presidio-analyzer is not "
                "installed. Run `uv sync --extra presidio`."
            ) from exc
        try:
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": cfg.language, "model_name": cfg.spacy_model}],
                }
            )
            engine = provider.create_engine()
        except OSError as exc:  # pragma: no cover - model not downloaded
            raise RuntimeError(
                f"spaCy model '{cfg.spacy_model}' is not installed. Run "
                f"`uv run python -m spacy download {cfg.spacy_model}`."
            ) from exc
        return AnalyzerEngine(nlp_engine=engine, supported_languages=[cfg.language])

    def find(self, text: str) -> list[PiiSpan]:
        if not text.strip():
            return []
        results = self.analyzer.analyze(
            text=text,
            language=self.cfg.language,
            entities=self.cfg.entities or None,
            score_threshold=self.cfg.score_threshold,
        )
        return [
            PiiSpan(
                start=r.start,
                end=r.end,
                label=LABELS.get(r.entity_type, r.entity_type),
                score=float(r.score),
            )
            for r in results
        ]
