"""Pure ASGI middleware: a trace ID per request, kept for the whole (streamed) response.

With OpenTelemetry or Langfuse on (ADR 026), each request also gets a server span that
continues an incoming W3C ``traceparent``, and the trace ID becomes the span's trace ID, so
``trace_id`` in responses and logs finds the trace in your tracing backend. Without them, an
``X-Request-ID`` of 8-64 safe characters is reused as the trace ID, or a new one is made.
"""

from __future__ import annotations

import json
import logging
import re
import time

from chatbot.observability.logging import new_trace_id, reset_trace_id, set_trace_id
from chatbot.observability.tracing import Telemetry, observe, telemetry

log = logging.getLogger("chatbot.access")
_SAFE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UNTRACED = {"/healthz", "/livez", "/readyz"}  # probes: no span, metric or access log


class TraceMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        probe = path in UNTRACED
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        incoming = headers.get("x-request-id", "")
        method = scope.get("method", "")
        tel = Telemetry() if probe else telemetry()  # the no-op for probes
        with tel.span(
            method,
            server=True,
            headers=headers,
            attrs={"http.request.method": method, "url.path": path},
        ) as span:
            trace_id = span.trace_id or (incoming if _SAFE.match(incoming) else new_trace_id())
            if span.trace_id and incoming:
                span.set({"chatbot.request_id": incoming})
            token = set_trace_id(trace_id)
            started = time.perf_counter()
            status = 500

            started_response = False

            async def send_wrapper(message) -> None:
                nonlocal status, started_response
                if message["type"] == "http.response.start":
                    status = message["status"]
                    started_response = True
                    message.setdefault("headers", []).append((b"x-trace-id", trace_id.encode()))
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                # Not handled by the app: answer like every other error (JSON with the trace
                # ID) instead of Starlette's plain "Internal Server Error". A response already
                # under way (a stream) can't be replaced.
                if started_response:
                    raise
                log.error("unhandled error", exc_info=exc, extra={"path": path})
                span.fail(exc)
                status = 500
                body = json.dumps({"detail": "internal error", "trace_id": trace_id}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"x-trace-id", trace_id.encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            finally:
                seconds = time.perf_counter() - started
                route = getattr(scope.get("route"), "path", None)
                span.rename(f"{method} {route or path}")
                span.set({"http.route": route, "http.response.status_code": status})
                if status >= 500:
                    span.fail(f"HTTP {status}")
                if not probe:
                    self._record(method, path, route, status, seconds)
                reset_trace_id(token)

    @staticmethod
    def _record(method: str, path: str, route: str | None, status: int, seconds: float) -> None:
        observe(
            "http.server.request.duration",
            seconds,
            {
                "http.request.method": method,
                "http.route": route or "unmatched",
                "http.response.status_code": status,
            },
        )
        log.info(
            "request",
            extra={
                "method": method,
                "path": path,
                "status": status,
                "ms": round(seconds * 1000, 1),
            },
        )
