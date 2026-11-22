"""Weak-retrieval detection (ADR 006, ADR 011).

``min_score`` and ``min_hits`` always apply together: retrieval is strong only when at
least ``min_hits`` chunks have a gate score ``>= min_score``. The gate score is the one
named by ``retrieval.gate`` (dense cosine, BM25 or rerank), never a fused RRF rank.

Thresholds calibrated for the index and the exact retrieval pipeline (stored with the index)
win over config; until then the config values are provisional and a warning is logged,
because a score means nothing on its own.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from chatbot.core.settings import RetrievalSection
from chatbot.core.types import Calibration, ScoredChunk

log = logging.getLogger(__name__)

UNCALIBRATED_WARNING = (
    "retrieval thresholds are provisional: this index and retrieval pipeline are not calibrated"
)
_warned: set[str] = set()


class Assessment(BaseModel):
    strong: list[ScoredChunk]  # hits whose gate score clears min_score
    usable: list[ScoredChunk]  # hits that may go to the model: strong, or with no gate score
    weak: bool
    gate: str
    min_score: float
    min_hits: int
    calibrated: bool
    warnings: list[str] = []


def thresholds(cfg: RetrievalSection, calibration: Calibration | None) -> tuple[float, int, bool]:
    if cfg.thresholds_from == "index" and calibration is not None:
        return calibration.min_score, calibration.min_hits, True
    return cfg.min_score, cfg.min_hits, False


def split(
    hits: list[ScoredChunk], gate: str, min_score: float, min_hits: int
) -> tuple[list[ScoredChunk], list[ScoredChunk], bool]:
    """(strong, usable, weak). A hybrid hit found only by BM25 has no dense score: it can't
    count towards min_hits, but it may still be used once retrieval is strong."""
    strong, usable = [], []
    for h in hits:
        score = h.gate_score(gate)
        if score is None:
            usable.append(h)
        elif score >= min_score:
            strong.append(h)
            usable.append(h)
    return strong, usable, len(strong) < min_hits


def assess(
    hits: list[ScoredChunk],
    cfg: RetrievalSection,
    calibration: Calibration | None,
    gate: str | None = None,
    warn_key: str | None = None,
) -> Assessment:
    gate = gate or cfg.gate_signal()
    min_score, min_hits, calibrated = thresholds(cfg, calibration)
    warnings = []
    if not calibrated and cfg.thresholds_from == "index" and warn_key is not None:
        if warn_key not in _warned:  # once per process and scorer, not on every request
            _warned.add(warn_key)
            log.warning(UNCALIBRATED_WARNING, extra={"scorer": warn_key, "min_score": min_score})
        warnings.append(UNCALIBRATED_WARNING)
    strong, usable, weak = split(hits, gate, min_score, min_hits)
    return Assessment(
        strong=strong,
        usable=usable,
        weak=weak,
        gate=gate,
        min_score=min_score,
        min_hits=min_hits,
        calibrated=calibrated,
        warnings=warnings,
    )
