"""Metric functions. Pure, so they are easy to test and to reuse in your own scripts.

Retrieval metrics are per document, not per chunk: a hit is the first chunk of an expected
document, and several chunks of one document count once.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence


def ranked_documents(doc_ids: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for d in doc_ids:
        if d not in seen:
            seen.append(d)
    return seen


def hit_at(doc_ids: Sequence[str], expected: Sequence[str], k: int) -> float:
    return float(any(d in expected for d in ranked_documents(doc_ids)[:k]))


def recall_at(doc_ids: Sequence[str], expected: Sequence[str], k: int) -> float:
    if not expected:
        return 0.0
    found = set(ranked_documents(doc_ids)[:k]) & set(expected)
    return len(found) / len(set(expected))


def reciprocal_rank(doc_ids: Sequence[str], expected: Sequence[str]) -> float:
    for rank, d in enumerate(ranked_documents(doc_ids), start=1):
        if d in expected:
            return 1.0 / rank
    return 0.0


def includes_all(answer: str, phrases: Sequence[str]) -> float:
    text = answer.lower()
    return float(all(p.lower() in text for p in phrases))


def citation_precision(cited: Sequence[str], expected: Sequence[str]) -> float:
    docs = set(cited)
    if not docs:
        return 0.0
    return len(docs & set(expected)) / len(docs)


def mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[idx], 1)
