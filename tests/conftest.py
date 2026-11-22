from __future__ import annotations

import os
from pathlib import Path

import pytest

from chatbot.core import container
from chatbot.core.settings import Settings, load_settings
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore

ROOT = Path(__file__).resolve().parents[1]


def offline_settings(**overrides) -> Settings:
    base = {"auth": {"mode": "none"}}
    for key, value in overrides.items():
        base[key] = value
    return load_settings(root=ROOT, profile="offline", environ={}, overrides=base)


@pytest.fixture
def settings() -> Settings:
    return offline_settings()


@pytest.fixture
def fake_components(settings):
    """Every Protocol backed by an in-memory fake: no database, no network."""
    return container.build(
        settings,
        pool=None,
        answer_llm=FakeLLM(),
        cheap_llm=FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
    )


# ---------------------------------------------------------------- Postgres


def _pg_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = _pg_url()
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a Postgres database with pgvector >= 0.8")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return url


@pytest.fixture(scope="session")
async def pg_pool(pg_url):
    from chatbot.db import make_pool

    pool = make_pool(offline_settings(), url=pg_url)
    await pool.open(wait=True)
    yield pool
    await pool.close()


@pytest.fixture
async def db(pg_pool):
    """A clean database for each test."""
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'chunks_hnsw_%'"
        )
        for row in await cur.fetchall():
            await conn.execute(f'DROP INDEX IF EXISTS "{row["indexname"]}"')
        await conn.execute(
            "TRUNCATE user_facts, conversation_summaries, turns, conversations, jobs, chunks, "
            "documents, index_aliases, usage_records, "
            "vector_indexes RESTART IDENTITY CASCADE"
        )
    return pg_pool
