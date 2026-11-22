"""Word-overlap reranker: the share of the query's words found in each chunk.

No model, no network. It exists so tests, CI and the offline profile can exercise the rerank
step; use a cross-encoder or a hosted reranker for real relevance.
"""

from __future__ import annotations

import itertools

from chatbot.core.registry import registry
from chatbot.core.settings import RerankerProfile
from chatbot.core.types import ScoredChunk, Usage
from chatbot.providers.rerankers import apply_scores
from chatbot.retrieval.bm25 import tokenize


def overlap(query: str, text: str) -> float:
    q = tokenize(query)
    if not q:
        return 0.0
    words = tokenize(text)
    vocab = set(words)
    pairs = set(itertools.pairwise(words))
    unigram = sum(1 for w in set(q) if w in vocab) / len(set(q))
    qpairs = set(itertools.pairwise(q))
    bigram = sum(1 for p in qpairs if p in pairs) / len(qpairs) if qpairs else unigram
    return round(0.7 * unigram + 0.3 * bigram, 6)


class LexicalReranker:
    def __init__(self, model_id: str = "lexical-v1") -> None:
        self.signature = f"rerank[{model_id}]"

    async def rerank(
        self, query: str, hits: list[ScoredChunk], top_n: int
    ) -> tuple[list[ScoredChunk], Usage]:
        return apply_scores(hits, [overlap(query, h.chunk.text) for h in hits], top_n), Usage()


@registry.register("reranker", "lexical")
def make_lexical(profile: RerankerProfile) -> LexicalReranker:
    return LexicalReranker(profile.model)
