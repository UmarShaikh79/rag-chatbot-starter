"""Pure ASGI middleware: a trace ID per request, kept for the whole (streamed) response."""

from __future__ import annotations

import logging
import re
import time

from chatbot.observability.logging import new_trace_id, reset_trace_id, set_trace_id

log = logging.getLogger("chatbot.access")
_SAFE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class TraceMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode()
        trace_id = incoming if _SAFE.match(incoming) else new_trace_id()
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-trace-id", trace_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            reset_trace_id(token)
