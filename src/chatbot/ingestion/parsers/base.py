from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from chatbot.core.registry import registry
from chatbot.core.types import ParsedDocument, RawDocument, Section


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fallback_title(raw: RawDocument) -> str:
    if raw.title:
        return raw.title
    stem = PurePosixPath(raw.uri.split("://", 1)[-1]).stem
    return stem.replace("-", " ").replace("_", " ").strip().capitalize() or raw.id


def build(raw: RawDocument, sections: list[Section], title: str | None = None) -> ParsedDocument:
    sections = [s for s in sections if s.text.strip()]
    return ParsedDocument(
        id=raw.id,
        source=raw.source,
        uri=raw.uri,
        title=title or fallback_title(raw),
        sections=sections,
        metadata=raw.metadata,
        content_hash=content_hash(raw.content),
    )


def parser_for(media_type: str):  # -> Parser
    for name in registry.names("parser"):
        parser = registry.create("parser", name)
        if media_type in parser.media_types:
            return parser
    raise ValueError(f"No parser registered for media type '{media_type}'")
