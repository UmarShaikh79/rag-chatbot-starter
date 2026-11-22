from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from chatbot.api.deps import components
from chatbot.core.container import Components

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(c: Components = Depends(components)) -> JSONResponse:
    db = "skipped"
    if c.pool is not None:
        try:
            async with c.pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            db = "ok"
        except Exception:
            db = "unavailable"
    ok = db != "unavailable"
    return JSONResponse({"status": "ok" if ok else "degraded", "database": db}, 200 if ok else 503)


@router.get("/livez")
async def livez() -> JSONResponse:
    """Liveness: the process answers. Never checks dependencies, so a database outage
    does not make Kubernetes restart every pod."""
    return JSONResponse({"status": "ok"})


@router.get("/readyz")
async def readyz(c: Components = Depends(components)) -> JSONResponse:
    """Readiness: same check as /healthz (the database answers), for load balancers."""
    return await healthz(c)
