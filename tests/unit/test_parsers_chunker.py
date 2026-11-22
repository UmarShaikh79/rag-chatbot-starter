from pathlib import Path

import pytest

from chatbot.core import container
from chatbot.core.types import ParsedDocument, RawDocument, Section
from chatbot.ingestion.chunkers.recursive import RecursiveChunker, approx_tokens
from chatbot.ingestion.parsers import parser_for
from chatbot.ingestion.sources.files import media_type_for
from tests.conftest import ROOT

DOCS = ROOT / "samples" / "docs"
container.load_modules()


def raw(path: Path) -> RawDocument:
    return RawDocument(
        id=f"t:{path.name}",
        source="t",
        uri=f"file://{path.name}",
        media_type=media_type_for(path),
        content=path.read_bytes(),
    )


@pytest.mark.parametrize(
    ("name", "title", "phrase"),
    [
        ("refund-policy.md", "Refund policy", "within 30 days of purchase"),
        ("privacy-and-data.html", "Privacy and your data", "Frankfurt"),
        ("security-faq.docx", "Security FAQ", "SMS codes are not supported"),
        ("keyboard-shortcuts.pdf", "Keyboard shortcuts", "Ctrl+K opens quick search"),
    ],
)
def test_parsers(name, title, phrase):
    doc = parser_for(media_type_for(DOCS / name)).parse(raw(DOCS / name))
    assert doc.title == title
    text = "\n".join(s.text for s in doc.sections)
    assert phrase in text
    assert len(doc.content_hash) == 64


def test_html_drops_navigation():
    doc = parser_for("text/html").parse(raw(DOCS / "privacy-and-data.html"))
    assert all("Home | Docs" not in s.text for s in doc.sections)
    assert [s.heading for s in doc.sections][1] == "Where data is stored"


def test_markdown_keeps_headings_as_sections():
    doc = parser_for("text/markdown").parse(raw(DOCS / "plans-and-billing.md"))
    assert [s.heading for s in doc.sections] == [
        "Plans",
        "Payment methods",
        "Changing plans",
        "Invoices",
    ]


def _doc(text: str, sections: int = 1) -> ParsedDocument:
    return ParsedDocument(
        id="d1",
        source="t",
        uri="u",
        title="T",
        sections=[Section(heading=f"H{i}", text=text) for i in range(sections)],
        content_hash="h",
    )


def test_chunks_respect_size_and_overlap():
    sentences = [f"Sentence number {i} talks about topic {i % 7}." for i in range(400)]
    chunker = RecursiveChunker(size_tokens=128, overlap_tokens=24)
    chunks = chunker.split(_doc(" ".join(sentences)))
    assert len(chunks) > 5
    assert all(approx_tokens(c.text) <= 128 for c in chunks)
    # Consecutive chunks share text (overlap carried over).
    first_body = chunks[0].text.split("\n", 1)[1]
    second_body = chunks[1].text.split("\n", 1)[1]
    assert first_body.splitlines()[-1] in second_body.splitlines()[:3]


def test_chunk_ids_are_deterministic_and_versioned():
    a = RecursiveChunker(128, 16).split(_doc("word " * 2000))
    b = RecursiveChunker(128, 16).split(_doc("word " * 2000))
    c = RecursiveChunker(256, 16).split(_doc("word " * 2000))
    assert [x.id for x in a] == [x.id for x in b]
    assert a[0].id != c[0].id
    assert len({x.id for x in a}) == len(a)


def test_sections_are_not_merged():
    chunks = RecursiveChunker(512, 64).split(_doc("short text", sections=3))
    assert [c.metadata["heading"] for c in chunks] == ["H0", "H1", "H2"]
    assert chunks[0].text.startswith("T > H0\n")


def test_giant_word_is_hard_split():
    chunks = RecursiveChunker(64, 8).split(_doc("x" * 5000))
    assert all(approx_tokens(c.text) <= 64 for c in chunks)
    assert sum(len(c.text.split("\n", 1)[1]) for c in chunks) >= 5000
