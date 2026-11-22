"""The golden dataset: one JSON object per line (evals/datasets/golden.jsonl)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    answerable: bool
    expected_documents: list[str] = []  # document IDs that hold the answer
    # Phrases a correct answer contains. An entry may be a list of alternatives, any one of
    # which counts. Matched after metrics.normalize: case, quotes, markdown and contractions
    # don't matter.
    must_include: list[str | list[str]] = []
    tags: list[str] = []  # free labels for slicing reports, e.g. ["exact-term"]

    @model_validator(mode="after")
    def _consistent(self) -> GoldenItem:
        if self.answerable and not self.expected_documents:
            raise ValueError(f"{self.id}: answerable questions need expected_documents")
        expects_an_answer = bool(self.expected_documents or self.must_include)
        if not self.answerable and expects_an_answer:
            raise ValueError(f"{self.id}: unanswerable questions take no expected answer")
        return self


def load_dataset(path: Path) -> list[GoldenItem]:
    items, seen = [], set()
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            item = GoldenItem.model_validate(json.loads(line))
        except Exception as exc:
            raise ValueError(f"{path}:{n}: {exc}") from exc
        if item.id in seen:
            raise ValueError(f"{path}:{n}: duplicate id {item.id}")
        seen.add(item.id)
        items.append(item)
    if not items:
        raise ValueError(f"{path} holds no questions")
    return items


def dataset_fingerprint(path: Path) -> str:
    """``golden.jsonl@1a2b3c4d``: recorded with a calibration so you can tell what it saw."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    return f"{path.name}@{digest}"
