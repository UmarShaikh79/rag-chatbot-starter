"""Offline embedder: hashed bag of word unigrams and bigrams, L2-normalised.

Lexical only, so it finds shared words, not meaning. It exists so tests, CI and the
offline demo profile run without network access. Do not use it in production.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re

from chatbot.core.registry import registry
from chatbot.core.settings import EmbeddingProfile
from chatbot.core.types import Usage

_WORD = re.compile(r"[a-z0-9]+")
# fmt: off
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "i", "if", "in", "is", "it", "my", "of", "on", "or", "our", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "will", "with", "you", "your",
})
# fmt: on


class HashingEmbedder:
    def __init__(self, dims: int = 384, model_id: str = "hashing-v1") -> None:
        self.dims = dims
        self.model_id = model_id

    def _vector(self, text: str) -> list[float]:
        words = [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS]
        feats = words + [f"{a}_{b}" for a, b in itertools.pairwise(words)]
        vec = [0.0] * self.dims
        for feat in feats:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            vec[h % self.dims] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        tokens = sum(len(t) // 4 for t in texts)
        return [self._vector(t) for t in texts], Usage(embedding_tokens=tokens)

    async def embed_query(self, text: str) -> tuple[list[float], Usage]:
        return self._vector(text), Usage(embedding_tokens=len(text) // 4)


@registry.register("embedder", "hashing")
def make_hashing_embedder(profile: EmbeddingProfile) -> HashingEmbedder:
    return HashingEmbedder(profile.dims, profile.model)
