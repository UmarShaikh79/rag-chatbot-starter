"""Markdown and plain text. Markdown headings become section boundaries."""

from __future__ import annotations

import re

from chatbot.core.registry import registry
from chatbot.core.types import ParsedDocument, RawDocument, Section
from chatbot.ingestion.parsers.base import build, normalise

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class MarkdownParser:
    media_types = ("text/markdown", "text/plain")

    def parse(self, raw: RawDocument) -> ParsedDocument:
        text = normalise(raw.content.decode("utf-8", errors="replace"))
        sections: list[Section] = []
        title: str | None = None
        heading: str | None = None
        buf: list[str] = []
        in_fence = False
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                in_fence = not in_fence
            m = None if in_fence else _HEADING.match(line)
            if m:
                if buf:
                    sections.append(Section(heading=heading, text="\n".join(buf).strip()))
                    buf = []
                heading = m.group(2).strip()
                if title is None and len(m.group(1)) == 1:
                    title = heading
                continue
            buf.append(line)
        if buf:
            sections.append(Section(heading=heading, text="\n".join(buf).strip()))
        return build(raw, sections, title)


registry.add("parser", "markdown", MarkdownParser)
