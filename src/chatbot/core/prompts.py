"""Prompt registry: versioned YAML templates under prompts/<name>/v<N>.yaml.

Reference a prompt as ``answer/v1``. Rendering is strict: every declared variable must be
provided and no undeclared placeholder may appear, so template drift fails loudly in tests.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from chatbot.core.types import Message


class PromptTemplate(BaseModel):
    name: str
    version: int
    description: str = ""
    model_role: str = "answer"
    variables: list[str] = []
    system: str
    user: str
    output: dict[str, Any] = {}

    @property
    def ref(self) -> str:
        return f"{self.name}/v{self.version}"

    def placeholders(self) -> set[str]:
        found: set[str] = set()
        for text in (self.system, self.user):
            for _, field, _, _ in string.Formatter().parse(text):
                if field:
                    found.add(field)
        return found

    def render(self, **values: Any) -> list[Message]:
        missing = set(self.variables) - values.keys()
        if missing:
            raise KeyError(f"Prompt {self.ref} is missing variables: {sorted(missing)}")
        unknown = self.placeholders() - set(self.variables)
        if unknown:
            raise KeyError(f"Prompt {self.ref} uses undeclared variables: {sorted(unknown)}")
        clean = {k: str(v) for k, v in values.items()}
        return [
            Message(role="system", content=self.system.format_map(clean).strip()),
            Message(role="user", content=self.user.format_map(clean).strip()),
        ]


class PromptRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, ref: str) -> PromptTemplate:
        if ref not in self._cache:
            name, _, version = ref.partition("/")
            if not version.startswith("v"):
                raise ValueError(f"Prompt reference must look like 'answer/v1', got '{ref}'")
            path = self.root / name / f"{version}.yaml"
            if not path.exists():
                raise FileNotFoundError(f"Prompt {ref} not found at {path}")
            self._cache[ref] = PromptTemplate.model_validate(yaml.safe_load(path.read_text()))
        return self._cache[ref]

    def all(self) -> list[PromptTemplate]:
        return [self.get(f"{p.parent.name}/{p.stem}") for p in sorted(self.root.glob("*/v*.yaml"))]
