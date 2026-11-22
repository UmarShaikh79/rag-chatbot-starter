from __future__ import annotations

import io

from pypdf import PdfReader

from chatbot.core.registry import registry
from chatbot.core.types import ParsedDocument, RawDocument, Section
from chatbot.ingestion.parsers.base import build, normalise


class PdfParser:
    """One section per page, so citations can point at a page number.

    Scanned PDFs have no text layer; add OCR in your own parser if you need it.
    """

    media_types = ("application/pdf",)

    def parse(self, raw: RawDocument) -> ParsedDocument:
        reader = PdfReader(io.BytesIO(raw.content))
        sections = [
            Section(text=normalise(page.extract_text() or ""), page=i + 1)
            for i, page in enumerate(reader.pages)
        ]
        title = None
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
        return build(raw, sections, title)


registry.add("parser", "pdf", PdfParser)
