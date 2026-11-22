"""Metric functions. Pure, so they are easy to test and to reuse in your own scripts.

Retrieval metrics are per document, not per chunk: a hit is the first chunk of an expected
document, and several chunks of one document count once.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
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


# Contractions a model writes where a golden phrase spells words out ("aren't refunded" must
# match "not refunded"). Irregular forms first, then the regular n't.
_CONTRACTIONS = [
    (re.compile(r"\bcan't\b"), "cannot"),
    (re.compile(r"\bwon't\b"), "will not"),
    (re.compile(r"\bshan't\b"), "shall not"),
    (re.compile(r"n't\b"), " not"),
    (re.compile(r"'re\b"), " are"),
    (re.compile(r"'ll\b"), " will"),
    (re.compile(r"'ve\b"), " have"),
]


def normalize(text: str) -> str:
    """Lower case, plain quotes and dashes, no markdown emphasis, contractions spelled out,
    single spaces. What ``must_include`` phrases and answers are compared in."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u201c": '"',
                "\u201d": '"',
                "\u2013": "-",
                "\u2014": "-",
            }
        )
    )
    text = re.sub(r"[*_`]+", "", text)
    for pattern, replacement in _CONTRACTIONS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def includes_all(answer: str, phrases: Sequence[str | Sequence[str]]) -> float:
    """1.0 when the answer contains every phrase. A phrase may be a list of alternatives, any
    one of which counts (["not refunded", "no refund"]). Compared after ``normalize``, so case,
    curly quotes, markdown and contractions ("aren't") don't matter."""
    text = normalize(answer)

    def found(phrase: str | Sequence[str]) -> bool:
        options = [phrase] if isinstance(phrase, str) else list(phrase)
        return any(normalize(o) in text for o in options)

    return float(all(found(p) for p in phrases))


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
