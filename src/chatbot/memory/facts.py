"""Helpers for long-term facts: parsing the extractor's reply and normalising for dedup."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel


class ExtractedFacts(BaseModel):
    facts: list[str] = []


def fact_key(fact: str) -> str:
    """Case, accents, punctuation and spacing don't make a fact new."""
    text = unicodedata.normalize("NFKD", fact).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9\[\] ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def parse_facts(raw: str) -> list[str]:
    """Accepts {"facts": [...]} alone or inside prose / code fences. Bad JSON -> []."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
        facts = ExtractedFacts.model_validate(data).facts
    except (ValueError, TypeError):
        return []
    return [f.strip() for f in facts if isinstance(f, str) and f.strip()]
