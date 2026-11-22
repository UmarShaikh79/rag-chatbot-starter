"""Regenerate the sample DOCX and PDF files: ``uv run python samples/make_binaries.py``.

The PDF is written by hand (no extra dependency) with one text line per row.
"""

from __future__ import annotations

from pathlib import Path

import docx

HERE = Path(__file__).parent / "docs"

SECURITY = [
    ("Title", "Security FAQ"),
    ("Normal", "Answers to common security questions about Tidepool Notes, a fictional product."),
    ("Heading 1", "Two-factor authentication"),
    (
        "Normal",
        "Two-factor authentication (2FA) can be turned on under Settings > Security. "
        "Tidepool supports authenticator apps and hardware security keys. SMS codes are not supported.",
    ),
    ("Normal", "Team admins can require 2FA for every member of a team space."),
    ("Heading 1", "Lost access"),
    (
        "Normal",
        "When you turn on 2FA you receive 10 one-time recovery codes. Each code works once. "
        "If you lose both your device and your recovery codes, support needs a government ID check, "
        "which takes up to 5 business days.",
    ),
    ("Heading 1", "Sessions"),
    (
        "Normal",
        "Web sessions expire after 14 days of inactivity. You can sign out all devices from "
        "Settings > Security > Active sessions.",
    ),
    ("Heading 1", "Reporting a vulnerability"),
    (
        "Normal",
        "Security researchers can report vulnerabilities through the responsible disclosure program. "
        "We reply within 3 business days.",
    ),
]

SHORTCUTS = [
    "Keyboard shortcuts",
    "Tidepool Notes desktop app (fictional product). On macOS use Cmd instead of Ctrl.",
    "Ctrl+N creates a new note.",
    "Ctrl+Shift+N creates a new notebook.",
    "Ctrl+K opens quick search across all notebooks.",
    "Ctrl+Shift+T adds a tag to the current note.",
    "Ctrl+Alt+S forces a sync.",
    "Ctrl+Shift+E exports the current note as Markdown.",
    "Shortcuts can be changed under Settings > Keyboard.",
]


def make_docx(path: Path) -> None:
    document = docx.Document()
    document.core_properties.title = "Security FAQ"
    for style, text in SECURITY:
        document.add_paragraph(text, style=style)
    document.save(path)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, lines: list[str], title: str) -> None:
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 760 Td"]
    for i, line in enumerate(lines):
        ops.append(
            f"({_escape(line)}) Tj T*" if i else f"/F1 16 Tf ({_escape(line)}) Tj T* /F1 11 Tf"
        )
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (" + _escape(title).encode("latin-1") + b") >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(out))


if __name__ == "__main__":
    make_docx(HERE / "security-faq.docx")
    make_pdf(HERE / "keyboard-shortcuts.pdf", SHORTCUTS, "Keyboard shortcuts")
    print("wrote security-faq.docx and keyboard-shortcuts.pdf")
