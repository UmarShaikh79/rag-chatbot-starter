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


def priced_mixed(
    usage: Usage, llm_prices: dict[str, float | None], embed_prices: dict[str, float | None]
) -> Usage:
    """Usage holding both model tokens and embedding tokens (retrieval with HyDE, say)."""
    parts = []
    if usage.prompt_tokens or usage.completion_tokens:
        parts.append(priced(usage.model_copy(update={"embedding_tokens": 0}), llm_prices))
    if usage.embedding_tokens:
        parts.append(priced(usage, embed_prices, embedding=True))
    costs = [p.cost_usd for p in parts]
    cost = None if not parts or any(c is None for c in costs) else sum(costs)  # type: ignore[arg-type]
    return usage.model_copy(update={"cost_usd": cost})
