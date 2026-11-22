"""Every relative link in the Markdown docs points at a file that exists, and every #anchor
at a heading that exists, the way GitHub renders them.

The guides cross-reference each other and the code heavily; this keeps a renamed file or a
reworded heading from leaving dead links behind.
"""

import re
from pathlib import Path

import pytest

from tests.conftest import ROOT

DOCS = sorted(
    [
        *ROOT.glob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
        ROOT / "evals" / "README.md",
        ROOT / "deploy" / "k8s" / "README.md",
    ]
)
_FENCE = re.compile(r"^(```|~~~)")
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def _outside_code(text: str) -> list[str]:
    """Lines that are not inside fenced code blocks, with inline code removed."""
    lines, in_fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(re.sub(r"`[^`]*`", "", line) if "](" in line else line)
    return lines


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [text](link) -> text
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in _outside_code(path.read_text()):
        m = _HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(1))
        count = seen.get(base, 0)
        out.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return out


def links(path: Path) -> list[str]:
    found = []
    for line in _outside_code(path.read_text()):
        found.extend(_LINK.findall(line))
    return [
        link
        for link in found
        if not re.match(r"^[a-z][a-z0-9+.-]*:", link)  # http:, https:, mailto: ...
    ]


def test_slug_matches_github():
    assert slug("`min_score` and `min_hits`") == "min_score-and-min_hits"
    assert slug("Streaming contract (ADR 014)") == "streaming-contract-adr-014"
    assert slug("4. One chat request, from start to finish") == (
        "4-one-chat-request-from-start-to-finish"
    )
    assert slug("Quick start (no API key needed)") == "quick-start-no-api-key-needed"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_links_resolve(doc: Path):
    broken = []
    for link in links(doc):
        target, _, anchor = link.partition("#")
        path = (doc.parent / target).resolve() if target else doc
        if not path.exists():
            broken.append(f"{link} (no such file)")
            continue
        if anchor and path.suffix == ".md" and anchor not in anchors(path):
            broken.append(f"{link} (no heading #{anchor} in {path.name})")
    assert not broken, f"{doc.relative_to(ROOT)}: " + "; ".join(broken)
