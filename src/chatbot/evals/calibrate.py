"""Calibrate ``min_score`` and ``min_hits`` together on the golden set (ADR 011, ADR 021).

For every candidate pair the sweep replays the retrieval pass and asks two questions:

* answerable questions: is retrieval strong AND does an expected document reach the model
  (among the first ``max_context_chunks`` usable hits)? That share is *answer recall*.
* unanswerable questions: is retrieval weak, so the bot asks instead of answering? That
  share is *abstention*.

It keeps the pairs with answer recall >= ``target_answer_recall`` (or, if none reach it, the
best recall found) and among those picks the highest abstention, then the highest recall,
then the widest margin. Thresholds sit midway between observed scores, so a tiny score
drift does not flip a question.

The result is stored with the index under the pipeline's scorer key. Calibrating and
evaluating on the same questions flatters the numbers; with a larger set, keep a held-out
part for ``chatbot eval``.
"""

from __future__ import annotations

import itertools

from pydantic import BaseModel

from chatbot.evals.runner import RetrievalRow
from chatbot.retrieval.weak import split


class SweepPoint(BaseModel):
    min_score: float
    min_hits: int
    answer_recall: float
    abstention: float
    margin: float


class CalibrationResult(BaseModel):
    best: SweepPoint
    met_target: bool
    target_answer_recall: float
    n_answerable: int
    n_unanswerable: int
    warnings: list[str] = []
    frontier: list[SweepPoint] = []  # best point per min_hits, for the report


def _candidates(rows: list[RetrievalRow]) -> list[tuple[float, float]]:
    """(threshold, margin): midpoints between neighbouring observed gate scores."""
    observed: set[float] = set()
    for row in rows:
        for hit in row.hits:
            if hit.gate_score is not None:
                observed.add(hit.gate_score)
    scores = sorted(observed)
    if not scores:
        return [(0.0, 0.0)]
    out = [(scores[0] - 1e-6, 0.0)]  # everything passes
    for lo, hi in itertools.pairwise(scores):
        out.append((round((lo + hi) / 2, 6), (hi - lo) / 2))
    out.append((scores[-1] + 1e-6, 0.0))  # nothing passes
    return out


def evaluate_point(
    rows: list[RetrievalRow], min_score: float, min_hits: int, max_context: int
) -> tuple[float, float]:
    """(answer recall, abstention) for one threshold pair."""
    served = abstained = 0
    n_ans = sum(r.answerable for r in rows)
    n_un = len(rows) - n_ans
    for r in rows:
        # Rebuild ScoredChunk-like objects cheaply: split() only needs gate_score().
        hits = [_Hit(h.document_id, h.gate_score) for h in r.hits]
        _, usable, weak = split(hits, "gate", min_score, min_hits)  # type: ignore[arg-type]
        if r.answerable:
            docs = [h.document_id for h in usable[:max_context]]
            served += int(not weak and any(d in r.expected for d in docs))
        else:
            abstained += int(weak)
    return (served / n_ans if n_ans else 0.0, abstained / n_un if n_un else 0.0)


class _Hit:
    __slots__ = ("_score", "document_id")

    def __init__(self, document_id: str, score: float | None) -> None:
        self.document_id = document_id
        self._score = score

    def gate_score(self, gate: str) -> float | None:
        return self._score


def sweep(
    rows: list[RetrievalRow],
    max_context: int,
    target_answer_recall: float = 0.9,
    max_min_hits: int = 3,
    min_unanswerable: int = 5,
) -> CalibrationResult:
    if not rows:
        raise ValueError("calibration needs at least one question")
    points = []
    for threshold, margin in _candidates(rows):
        for hits in range(1, max_min_hits + 1):
            recall, abstention = evaluate_point(rows, threshold, hits, max_context)
            points.append(
                SweepPoint(
                    min_score=round(threshold, 6),
                    min_hits=hits,
                    answer_recall=round(recall, 4),
                    abstention=round(abstention, 4),
                    margin=round(margin, 6),
                )
            )
    best_recall = max(p.answer_recall for p in points)
    floor = min(target_answer_recall, best_recall)
    eligible = [p for p in points if p.answer_recall >= floor]

    def rank(p: SweepPoint) -> tuple[float, float, float, int]:
        return (p.abstention, p.answer_recall, p.margin, -p.min_hits)

    best = max(eligible, key=rank)
    # For the report: the best point for each min_hits value, to show the trade-off.
    frontier = []
    for hits in range(1, max_min_hits + 1):
        with_these_hits = [p for p in eligible if p.min_hits == hits]
        if with_these_hits:
            frontier.append(max(with_these_hits, key=rank))
    n_ans = sum(r.answerable for r in rows)
    n_un = len(rows) - n_ans
    warnings = []
    if best_recall < target_answer_recall:
        warnings.append(
            f"no threshold keeps {target_answer_recall:.0%} of answerable questions "
            f"(best {best_recall:.0%}): retrieval misses expected documents, fix that first"
        )
    if n_un < min_unanswerable:
        warnings.append(
            f"only {n_un} unanswerable questions: add more so abstention is measured, not guessed"
        )
    if best.abstention == 0 and n_un:
        warnings.append(
            "no threshold separates unanswerable questions at this recall; the answer "
            "prompt's INSUFFICIENT_CONTEXT path is the only safety net"
        )
    return CalibrationResult(
        best=best,
        met_target=best_recall >= target_answer_recall,
        target_answer_recall=target_answer_recall,
        n_answerable=n_ans,
        n_unanswerable=n_un,
        warnings=warnings,
        frontier=frontier,
    )
