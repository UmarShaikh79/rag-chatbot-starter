from __future__ import annotations

from fastapi import APIRouter, Depends

from chatbot.api.deps import components
from chatbot.core.container import Components
from chatbot.core.types import DeletionReport

router = APIRouter(prefix="/v1", tags=["memory"])


@router.delete("/users/{user_id}/memory", response_model=DeletionReport)
async def forget_user(user_id: str, c: Components = Depends(components)) -> DeletionReport:
    """Delete every turn, conversation, queued job and owned document for this user."""
    return await c.memory.forget_user(user_id)
