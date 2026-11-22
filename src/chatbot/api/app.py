"""FastAPI app factory. ``uvicorn chatbot.api.app:create_app --factory``."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chatbot import __version__
from chatbot.api.middleware import UNTRACED, TraceMiddleware
from chatbot.api.routes import chat, health, ingest, memory, usage
from chatbot.auth.api_key import Authenticator
from chatbot.auth.principal import require_admin
from chatbot.core import container
from chatbot.core.errors import (
    ChatbotError,
    ForbiddenError,
    IndexMismatchError,
    NotFoundError,
    describe_error,
)
from chatbot.core.settings import Settings, get_settings
from chatbot.db import make_pool
from chatbot.observability.logging import configure_logging, get_trace_id
from chatbot.observability.tracing import configure_telemetry, shutdown_telemetry

log = logging.getLogger(__name__)


def _is_probe(scope: Any) -> bool:
    return scope.get("type") == "http" and scope.get("path") in UNTRACED


def _fastapi_telemetry() -> dict[str, Any]:
    """FastAPI 0.165+ traces and meters requests itself once an OpenTelemetry provider exists
    (ADR 026 sets one). TraceMiddleware already owns the server span (continuing
    ``traceparent``) and the HTTP metric, so FastAPI's would duplicate them: a second, empty
    root trace per request, every request counted twice, and a trace per health check. Turn
    its request spans and metrics off and skip probes entirely; its dependency, endpoint and
    serialization spans still nest under the template's server span. Older FastAPI has no
    ``telemetry`` argument and nothing to turn off."""
    if "telemetry" not in inspect.signature(FastAPI.__init__).parameters:
        return {}
    return {"telemetry": {"tracing": False, "metrics": False, "exclude": _is_probe}}


def create_app(
    settings: Settings | None = None,
    components: container.Components | None = None,
    auth: Authenticator | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.observability.log_level, settings.observability.log_format)
    auth = auth or Authenticator(settings.auth)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = components is None
        if owned:
            configure_telemetry(settings, "api")  # no-op unless otel or langfuse is on
            pool = make_pool(settings)
            await pool.open(wait=True, timeout=30)
            app.state.components = container.build(settings, pool)
        else:
            app.state.components = components
        log.info(
            "api started",
            extra={"profile_env": settings.app.env, "index": settings.vector_store.index},
        )
        try:
            yield
        finally:
            if owned:
                await app.state.components.close()
                shutdown_telemetry()

    app = FastAPI(
        title=settings.app.name,
        version=__version__,
        lifespan=lifespan,
        description="RAG chatbot backend. Integrate from any stack over HTTP/SSE.",
        **_fastapi_telemetry(),
    )
    app.add_middleware(TraceMiddleware)
    protected = [Depends(auth)]
    # End users signed in with a JWT can chat and manage their own memory; ingestion,
    # documents and jobs need an API key or a token with the admin scope.
    admin_only = [Depends(auth), Depends(require_admin)]
    app.include_router(health.router)
    app.include_router(chat.router, dependencies=protected)
    app.include_router(ingest.router, dependencies=admin_only)
    app.include_router(memory.router, dependencies=protected)
    app.include_router(usage.router, dependencies=admin_only)

    def error(status: int, message: Any, headers: Mapping[str, str] | None = None) -> JSONResponse:
        return JSONResponse(
            {"detail": message, "trace_id": get_trace_id()}, status_code=status, headers=headers
        )

    # Raised by routes and dependencies (auth 401/403/503, 404s): the same body as the
    # errors below, so every error carries the trace ID. Headers (WWW-Authenticate,
    # Retry-After) are kept.
    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error(exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        return error(403, str(exc))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return error(404, str(exc))

    @app.exception_handler(IndexMismatchError)
    async def _mismatch(_: Request, exc: IndexMismatchError) -> JSONResponse:
        return error(409, str(exc))

    # Any other failure of ours or of a provider: 502 when the provider failed (the request
    # was fine), 500 otherwise, with a message safe to show (describe_error). Anything not
    # handled here becomes a JSON 500 in TraceMiddleware.
    async def _failed(_: Request, exc: Exception) -> JSONResponse:
        status, message = describe_error(exc)
        log.error("request failed", exc_info=exc, extra={"status": status})
        return error(status, message)

    provider_errors: list[type[Exception]] = [ChatbotError, httpx.HTTPError]
    try:
        from openai import OpenAIError

        provider_errors.append(OpenAIError)
    except ImportError:
        pass
    for kind in provider_errors:
        app.add_exception_handler(kind, _failed)

    return app
