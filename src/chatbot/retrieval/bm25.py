"""Keyword retrieval with Okapi BM25 (ADR 020).

The retriever asks the vector store for ``keyword_search``: the pgvector store scores BM25 in
SQL over a ``tsvector`` column, so nothing extra runs or needs installing. ``bm25_rank``
below is the same formula in plain Python, used by the in-memory store in tests and by
anyone who wants BM25 over a small list of texts.

BM25 finds exact terms that embeddings blur: error codes (``E309``), product names, IDs.
Its scores are unbounded and depend on the corpus, so they are only comparable within one
index, and thresholds on them must be calibrated like any other (ADR 011).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any

from chatbot.core.errors import ChatbotError
from chatbot.core.interfaces import Embedder, KeywordSearch, VectorStore
from chatbot.core.registry import registry
from chatbot.core.types import IndexInfo, ScoredChunk, Usage
from chatbot.retrieval.dense import check_index

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")
# fmt: off
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "i", "if", "in", "is", "it", "my", "of", "on", "or", "our", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "will", "with", "you", "your",
})
# fmt: on


def tokenize(text: str) -> list[str]:
    """Lower-case words without stopwords. No stemming: keep it predictable for tests."""
    return [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS]


def bm25_rank(
    query: str, docs: list[str], k1: float = 1.2, b: float = 0.75
) -> list[tuple[int, float]]:
    """(doc position, score) for docs sharing a term with the query, best first.

    For each query term found in a document, BM25 adds::

        idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_length / average_length))

    - ``tf``: how often the term occurs in this document. More is better, but with
      diminishing returns: ``k1`` sets how fast extra occurrences stop counting.
    - ``idf``: how rare the term is across all documents. "E309" in 1 of 1,000 documents is
      worth far more than "account" in 900 of them.
    - ``b``: how much a long document is penalised (0 = not at all, 1 = fully), so a long page
      doesn't win just because it contains more words.
    """
    terms = set(tokenize(query))
    if not terms or not docs:
        return []
    tokenized = [tokenize(d) for d in docs]
    n = len(docs)
    average_length = sum(len(t) for t in tokenized) / n or 1.0

    # Document frequency: in how many documents each query term appears (once per document).
    df: Counter[str] = Counter()
    for toks in tokenized:
        for t in terms & set(toks):
            df[t] += 1

    ranked = []
    for i, toks in enumerate(tokenized):
        tf = Counter(toks)
        length_norm = 1 - b + b * len(toks) / average_length
        score = 0.0
        for t in terms:
            if not tf[t]:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * length_norm)
        if score > 0:
            ranked.append((i, score))
    ranked.sort(key=lambda x: (-x[1], x[0]))  # best score first; ties keep document order
    return ranked


class Bm25Retriever:
    name = "bm25"
    signature = "bm25"

    def __init__(self, embedder: Embedder, store: VectorStore, alias: str) -> None:
        if not isinstance(store, KeywordSearch):
            raise ChatbotError(
                f"retrieval uses bm25 but the vector store {type(store).__name__} has no "
                "keyword_search; use the pgvector store, set retrieval.strategy: dense, or "
                "add keyword_search to your store (ADR 024)"
            )
        self.embedder = embedder
        self.store = store
        self.alias = alias

    async def index(self) -> IndexInfo | None:
        return await check_index(self.store, self.alias, self.embedder)

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        info = await self.index()
        if info is None:
            return [], Usage()
        hits = await self.store.keyword_search(info, query, k, filters)  # type: ignore[attr-defined]
        return hits, Usage()


@registry.register("retriever", "bm25")
def make_bm25(embedder: Embedder, store: VectorStore, alias: str, **_: Any) -> Bm25Retriever:
    return Bm25Retriever(embedder, store, alias)
