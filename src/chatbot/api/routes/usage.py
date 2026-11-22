"""Token and cost reports from usage_records (ADR 028). Admin only."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from chatbot.api.deps import components
from chatbot.core.container import Components
from chatbot.observability.accounting import GroupBy, UsageReport

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageReport)
async def usage(
    group_by: GroupBy = "day",
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: str | None = None,
    c: Components = Depends(components),
) -> UsageReport:
    """Tokens and cost summed by day (UTC), user, model or operation. ``cost_usd`` counts
    priced calls only: ``unpriced_calls`` says how many calls had no price in models.yaml."""
    if c.usage_store is None:
        raise HTTPException(
            404, "usage records are off: set observability.usage.persist (needs Postgres)"
        )
    return await c.usage_store.report(group_by, since, until, user_id)
