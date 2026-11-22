from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from chatbot.api.deps import components
from chatbot.auth.principal import Principal, current_principal
from chatbot.core.container import Components
from chatbot.core.interfaces import UserDocumentEraser
from chatbot.core.types import DeletionReport, UserFact

router = APIRouter(prefix="/v1", tags=["memory"])


class UserMemory(BaseModel):
    user_id: str
    facts: list[UserFact]


def _same_user(user_id: str, principal: Principal) -> None:
    if principal.is_end_user and principal.subject != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you can only manage your own memory")


@router.get("/users/{user_id}/memory", response_model=UserMemory)
async def get_memory(
    user_id: str,
    c: Components = Depends(components),
    principal: Principal = Depends(current_principal),
) -> UserMemory:
    """Long-term facts remembered about this user, most recently seen first."""
    _same_user(user_id, principal)
    return UserMemory(user_id=user_id, facts=await c.memory.list_facts(user_id, 1000))


@router.delete("/users/{user_id}/memory/facts/{fact_id}", status_code=204)
async def delete_fact(
    user_id: str,
    fact_id: int,
    c: Components = Depends(components),
    principal: Principal = Depends(current_principal),
) -> None:
    """Forget one fact."""
    _same_user(user_id, principal)
    if not await c.memory.delete_fact(user_id, fact_id):
        raise HTTPException(404, "fact not found")


@router.delete("/users/{user_id}/memory", response_model=DeletionReport)
async def forget_user(
    user_id: str,
    c: Components = Depends(components),
    principal: Principal = Depends(current_principal),
) -> DeletionReport:
    """Delete every fact, summary, turn, conversation, queued job and owned document for
    this user. Returns counts per table."""
    _same_user(user_id, principal)
    # Vectors kept outside Postgres (Qdrant) go first: if that fails, the rows that find
    # them are still there and the call can be retried (ADR 024).
    vectors = None
    if isinstance(c.store, UserDocumentEraser):
        vectors = await c.store.forget_user_documents(user_id)
    report = await c.memory.forget_user(user_id)
    if vectors is not None:
        report.deleted["vector_points"] = vectors
    return report
