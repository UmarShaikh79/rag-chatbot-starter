"""Citation alignment: map inline [n] markers to the sources the model was shown."""

from __future__ import annotations

import re

from chatbot.core.types import Citation, ScoredChunk

_MARKER = re.compile(r"\[(\d{1,3})\]")


def format_sources(hits: list[ScoredChunk], max_chars: int = 2400) -> str:
    lines = []
    for i, hit in enumerate(hits, start=1):
        meta = hit.chunk.metadata
        label = str(meta.get("title") or hit.chunk.document_id)
        if meta.get("heading"):
            label = f"{label} > {meta['heading']}"
        text = hit.chunk.text
        if text.startswith(label + "\n"):  # the chunker's breadcrumb line becomes the label
            text = text[len(label) + 1 :]
        if meta.get("page"):
            label += f", page {meta['page']}"
        lines.append(f"[{i}] ({label}) {' '.join(text.split())[:max_chars]}")
    return "\n\n".join(lines)


def align(text: str, hits: list[ScoredChunk]) -> tuple[str, list[Citation]]:
    """Drop markers that point at no source; return cleaned text and cited sources in order."""
    used: dict[int, Citation] = {}

    def keep(match: re.Match[str]) -> str:
        n = int(match.group(1))
        if not 1 <= n <= len(hits):
            return ""
        if n not in used:
            hit = hits[n - 1]
            meta = hit.chunk.metadata
            used[n] = Citation(
                marker=n,
                chunk_id=hit.chunk.id,
                document_id=hit.chunk.document_id,
                title=str(meta.get("title") or hit.chunk.document_id),
                uri=str(meta.get("uri") or ""),
                score=round(hit.score, 4),
                snippet=" ".join(hit.chunk.text.split())[:240],
            )
        return match.group(0)

    cleaned = _MARKER.sub(keep, text)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, sorted(used.values(), key=lambda c: c.marker)
