from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from chatbot.api.deps import components
from chatbot.auth.principal import Principal, current_principal
from chatbot.core.container import Components
from chatbot.core.types import ChatEvent, ChatRequest, ChatResponse

router = APIRouter(prefix="/v1", tags=["chat"])


def sse(event: ChatEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    summary="Ask a question. Set stream=true (or Accept: text/event-stream) for SSE.",
)
async def chat(
    req: ChatRequest,
    request: Request,
    c: Components = Depends(components),
    principal: Principal = Depends(current_principal),
):
    # A signed-in end user always chats as themselves (the token's subject).
    req = req.model_copy(update={"user_id": principal.resolve_user_id(req.user_id)})
    wants_stream = req.stream or "text/event-stream" in request.headers.get("accept", "")
    if not wants_stream:
        return await c.orchestrator.run(req)

    async def body() -> AsyncIterator[str]:
        async for event in c.orchestrator.stream(req):
            yield sse(event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
