"""Postgres connection pool (psycopg 3). Every Postgres-backed module shares one pool."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from chatbot.core.settings import Settings


def normalise_url(url: str) -> str:
    """Accept SQLAlchemy-style URLs too (postgresql+psycopg://...)."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def make_pool(settings: Settings, url: str | None = None) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        normalise_url(url or settings.database_url()),
        min_size=settings.database.pool_min,
        max_size=settings.database.pool_max,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )


def vector_literal(values: list[float]) -> str:
    """pgvector text format. Avoids a numpy dependency for adapting vectors."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
