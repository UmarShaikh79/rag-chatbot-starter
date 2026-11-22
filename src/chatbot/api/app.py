"""FastAPI app factory. ``uvicorn chatbot.api.app:create_app --factory``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from chatbot import __version__
from chatbot.api.middleware import TraceMiddleware
from chatbot.api.routes import chat, health, ingest, memory, usage
from chatbot.auth.api_key import Authenticator
from chatbot.auth.principal import require_admin
from chatbot.core import container
from chatbot.core.errors import ForbiddenError, IndexMismatchError, NotFoundError
from chatbot.core.settings import Settings, get_settings
from chatbot.db import make_pool
from chatbot.observability.logging import configure_logging, get_trace_id
from chatbot.observability.tracing import configure_telemetry, shutdown_telemetry

log = logging.getLogger(__name__)


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

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"detail": message, "trace_id": get_trace_id()}, status_code=status)

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        return error(403, str(exc))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return error(404, str(exc))

    @app.exception_handler(IndexMismatchError)
    async def _mismatch(_: Request, exc: IndexMismatchError) -> JSONResponse:
        return error(409, str(exc))

    return app
