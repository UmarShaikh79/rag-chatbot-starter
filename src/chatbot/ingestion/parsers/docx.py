from __future__ import annotations

import io

import docx

from chatbot.core.registry import registry
from chatbot.core.types import ParsedDocument, RawDocument, Section
from chatbot.ingestion.parsers.base import build, normalise


class DocxParser:
    media_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)

    def parse(self, raw: RawDocument) -> ParsedDocument:
        document = docx.Document(io.BytesIO(raw.content))
        title = document.core_properties.title or None
        sections: list[Section] = []
        heading: str | None = None
        buf: list[str] = []
        for para in document.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style.startswith("Heading") or style == "Title":
                if buf:
                    sections.append(Section(heading=heading, text=normalise("\n".join(buf))))
                    buf = []
                heading = text
                if title is None:
                    title = text
            else:
                buf.append(text)
        for table in document.tables:
            for row in table.rows:
                buf.append(" | ".join(c.text.strip() for c in row.cells))
        if buf:
            sections.append(Section(heading=heading, text=normalise("\n".join(buf))))
        return build(raw, sections, title)


registry.add("parser", "docx", DocxParser)
