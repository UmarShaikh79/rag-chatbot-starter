"""Token and cost accounting. Cost is only reported when prices are set in models.yaml."""

from __future__ import annotations

from chatbot.core.types import Usage


def priced(usage: Usage, prices: dict[str, float | None], *, embedding: bool = False) -> Usage:
    if embedding:
        price = prices.get("input")
        cost = None if price is None else usage.embedding_tokens * price / 1_000_000
    else:
        pin, pout = prices.get("input"), prices.get("output")
        cost = None
        if pin is not None and pout is not None:
            cost = (usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1_000_000
    return usage.model_copy(update={"cost_usd": cost})
