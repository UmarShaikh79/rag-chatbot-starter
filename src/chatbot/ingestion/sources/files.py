"""File source: walks a folder and yields every file that matches the include patterns."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path

from chatbot.core.registry import registry
from chatbot.core.settings import SourceConfig
from chatbot.core.types import RawDocument

MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def media_type_for(path: Path) -> str:
    known = MEDIA_TYPES.get(path.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


class FileSource:
    def __init__(self, name: str, root: Path, include: list[str]) -> None:
        self.name = name
        self.root = root
        self.include = include

    def paths(self) -> list[Path]:
        if not self.root.exists():
            raise FileNotFoundError(f"Source '{self.name}': folder {self.root} does not exist")
        found: set[Path] = set()
        for pattern in self.include:
            found.update(p for p in self.root.rglob(pattern) if p.is_file())
        return sorted(found)

    async def iter_documents(self) -> AsyncIterator[RawDocument]:
        for path in self.paths():
            rel = path.relative_to(self.root).as_posix()
            yield RawDocument(
                id=f"{self.name}:{rel}",
                source=self.name,
                uri=f"file://{rel}",
                title=None,
                media_type=media_type_for(path),
                content=path.read_bytes(),
                metadata={"path": rel},
            )


@registry.register("source", "files")
def make_file_source(cfg: SourceConfig, root_dir: Path) -> FileSource:
    if not cfg.path:
        raise ValueError(f"Source '{cfg.name}' needs a path")
    path = Path(cfg.path)
    return FileSource(cfg.name, path if path.is_absolute() else root_dir / path, cfg.include)
