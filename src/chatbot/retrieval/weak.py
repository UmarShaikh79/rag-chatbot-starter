"""Weak-retrieval detection (ADR 006, ADR 011).

``min_score`` and ``min_hits`` always apply together: retrieval is strong only when at
least ``min_hits`` chunks score ``>= min_score``. Thresholds calibrated for the index
(stored with it) win over config; until an index is calibrated the config values are
provisional and a warning is logged, because a cosine score means nothing on its own.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from chatbot.core.settings import RetrievalSection
from chatbot.core.types import IndexInfo, ScoredChunk

log = logging.getLogger(__name__)

UNCALIBRATED_WARNING = "retrieval thresholds are provisional: this index is not calibrated"
_warned: set[str] = set()


class Assessment(BaseModel):
    strong: list[ScoredChunk]
    weak: bool
    min_score: float
    min_hits: int
    calibrated: bool
    warnings: list[str] = []


def thresholds(cfg: RetrievalSection, index: IndexInfo | None) -> tuple[float, int, bool]:
    if (
        cfg.thresholds_from == "index"
        and index is not None
        and index.min_score is not None
        and index.min_hits is not None
    ):
        return index.min_score, index.min_hits, True
    return cfg.min_score, cfg.min_hits, False


def assess(hits: list[ScoredChunk], cfg: RetrievalSection, index: IndexInfo | None) -> Assessment:
    min_score, min_hits, calibrated = thresholds(cfg, index)
    warnings = []
    if not calibrated and cfg.thresholds_from == "index" and index is not None:
        if index.name not in _warned:  # once per process and index, not on every request
            _warned.add(index.name)
            log.warning(UNCALIBRATED_WARNING, extra={"index": index.name, "min_score": min_score})
        warnings.append(UNCALIBRATED_WARNING)
    strong = [h for h in hits if h.score >= min_score]
    return Assessment(
        strong=strong,
        weak=len(strong) < min_hits,
        min_score=min_score,
        min_hits=min_hits,
        calibrated=calibrated,
        warnings=warnings,
    )
