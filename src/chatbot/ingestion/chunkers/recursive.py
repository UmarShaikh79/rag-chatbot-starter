"""Structure-aware recursive chunker (default).

Sections (headings) are never merged. Inside a section, text is split on paragraphs, then
lines, then sentences, then words, and the pieces are packed into chunks of at most
``size_tokens`` with ``overlap_tokens`` carried over from the previous chunk.

Chunk IDs are derived from document ID, chunker version and position, so re-chunking the
same document with the same settings yields the same IDs (ADR 013).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable

from chatbot.core.registry import registry
from chatbot.core.settings import ChunkerSection
from chatbot.core.types import Chunk, ParsedDocument

_SEPARATORS = [r"\n\s*\n", r"\n", r"(?<=[.!?])\s+", r"\s+"]


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def make_counter(name: str) -> Callable[[str], int]:
    if name == "tiktoken":
        import tiktoken  # optional dependency

        enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text))
    return approx_tokens


def chunk_id(document_id: str, version: str, position: int) -> str:
    return hashlib.sha256(f"{document_id}|{version}|{position}".encode()).hexdigest()[:32]


class RecursiveChunker:
    def __init__(self, size_tokens: int = 512, overlap_tokens: int = 64, tokenizer: str = "approx"):
        self.size = size_tokens
        self.overlap = overlap_tokens
        self.count = make_counter(tokenizer)
        self.version = f"recursive-v1:{size_tokens}:{overlap_tokens}:{tokenizer}"

    # -- splitting ---------------------------------------------------------------------

    def _pieces(self, text: str, budget: int, level: int = 0) -> list[str]:
        """Split text into pieces that each fit in one chunk."""
        if self.count(text) <= budget:
            return [text]
        if level >= len(_SEPARATORS):
            # A single "word" longer than a chunk: hard cut by characters.
            step = max(1, len(text) * budget // self.count(text))
            return [text[i : i + step] for i in range(0, len(text), step)]
        out: list[str] = []
        for part in re.split(_SEPARATORS[level], text):
            if part.strip():
                out.extend(self._pieces(part.strip(), budget, level + 1))
        return out

    def _pack(self, pieces: list[str], budget: int) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        for piece in pieces:
            candidate = [*current, piece]
            if current and self.count("\n".join(candidate)) > budget:
                chunks.append("\n".join(current))
                tail: list[str] = []
                for prev in reversed(current):  # carry overlap from the end of the last chunk
                    if self.count("\n".join([prev, *tail])) > self.overlap:
                        break
                    tail.insert(0, prev)
                current = [*tail, piece]
                if self.count("\n".join(current)) > budget:
                    current = [piece]
            else:
                current = candidate
        if current:
            chunks.append("\n".join(current))
        return chunks

    # -- public ------------------------------------------------------------------------

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in doc.sections:
            crumb = doc.title if not section.heading else f"{doc.title} > {section.heading}"
            budget = max(16, self.size - self.count(crumb) - 1)
            bodies = self._pack(self._pieces(section.text, budget), budget)
            for body in bodies:
                text = f"{crumb}\n{body}"
                position = len(chunks)
                meta = {"title": doc.title, "heading": section.heading, **doc.metadata}
                if section.page is not None:
                    meta["page"] = section.page
                chunks.append(
                    Chunk(
                        id=chunk_id(doc.id, self.version, position),
                        document_id=doc.id,
                        position=position,
                        text=text,
                        metadata={k: v for k, v in meta.items() if v is not None},
                        content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    )
                )
        return chunks


@registry.register("chunker", "recursive")
def make_recursive_chunker(cfg: ChunkerSection) -> RecursiveChunker:
    return RecursiveChunker(cfg.size_tokens, cfg.overlap_tokens, cfg.tokenizer)
