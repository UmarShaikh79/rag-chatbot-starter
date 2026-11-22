from __future__ import annotations

from bs4 import BeautifulSoup

from chatbot.core.registry import registry
from chatbot.core.types import ParsedDocument, RawDocument, Section
from chatbot.ingestion.parsers.base import build, normalise

_HEADINGS = ["h1", "h2", "h3", "h4"]
_BLOCKS = ["p", "li", "td", "th", "pre", "blockquote", "dd", "dt"]


class HtmlParser:
    media_types = ("text/html",)

    def parse(self, raw: RawDocument) -> ParsedDocument:
        soup = BeautifulSoup(raw.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript", "template"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else None
        sections: list[Section] = []
        heading: str | None = None
        buf: list[str] = []
        body = soup.body or soup
        for el in body.find_all([*_HEADINGS, *_BLOCKS]):
            if el.find_parent(_BLOCKS):  # nested block, already counted by its parent
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            if el.name in _HEADINGS:
                if buf:
                    sections.append(Section(heading=heading, text=normalise("\n".join(buf))))
                    buf = []
                heading = text
                if title is None and el.name == "h1":
                    title = text
            else:
                buf.append(("- " if el.name == "li" else "") + text)
        if buf:
            sections.append(Section(heading=heading, text=normalise("\n".join(buf))))
        return build(raw, sections, title)


registry.add("parser", "html", HtmlParser)
