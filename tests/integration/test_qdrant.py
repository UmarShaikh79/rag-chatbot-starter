"""Qdrant store (ADR 024): the same contract as pgvector, plus moving an index between them.

Runs Qdrant's in-process local mode by default, or a real server when QDRANT_TEST_URL is
set (CI does both). Postgres is needed either way: it keeps the index catalog.
"""

import os
import random
import shutil
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client", reason="uv sync --extra qdrant")

from qdrant_client import AsyncQdrantClient

from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.errors import ChatbotError, IndexMismatchError
from chatbot.core.settings import QdrantSection
from chatbot.core.types import ChatRequest
from chatbot.ingestion.pipeline import InlineDocument
from chatbot.ingestion.reindex import reindex
from chatbot.memory.postgres import PostgresMemoryStore
from chatbot.stores.qdrant import QdrantStore
from tests.conftest import ROOT, offline_settings
from tests.integration.test_pgvector import DIMS, chunks, doc, unit_vec

pytestmark = [pytest.mark.integration, pytest.mark.qdrant]


@pytest.fixture
async def qclient():
    url = os.environ.get("QDRANT_TEST_URL")
    client = AsyncQdrantClient(url=url) if url else AsyncQdrantClient(location=":memory:")
    for c in (await client.get_collections()).collections:
        await client.delete_collection(c.name)
    yield client
    await client.close()


@pytest.fixture
def store(db, qclient) -> QdrantStore:
    return QdrantStore(db, qclient, QdrantSection())


async def test_index_lives_in_a_collection_and_refuses_mismatch(store, qclient):
    info = await store.ensure_index("docs", "model-a", DIMS, "v1")
    assert info.backend == "qdrant" and info.name == "docs__model_a__16"
    assert await qclient.collection_exists(info.name)
    assert (await store.ensure_index("docs", "model-a", DIMS, "v1")).id == info.id
    with pytest.raises(IndexMismatchError):
        await store.ensure_index("docs", "model-b", DIMS, "v1")
    with pytest.raises(IndexMismatchError):
        await store.search(info, [0.1] * (DIMS + 1), 3, {})


async def test_replace_leaves_no_stale_chunks(store):
    rng = random.Random(1)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    old = chunks("d1", 5, "old")
    await store.replace_document(info, doc("d1", h="h1"), old, [unit_vec(rng) for _ in old])
    new = chunks("d1", 3, "new")
    await store.replace_document(info, doc("d1", h="h2"), new, [unit_vec(rng) for _ in new])
    hits = await store.search(info, unit_vec(rng), 50, {})
    assert sorted(h.chunk.id for h in hits) == sorted(c.id for c in new)
    assert (await store.document_state(info, "d1"))["content_hash"] == "h2"
    assert await store.delete_document(info, "d1") == 1
    assert await store.search(info, unit_vec(rng), 50, {}) == []


async def test_filters_scores_and_payload(store):
    rng = random.Random(3)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    for i in range(4):
        cs = chunks(f"d{i}", 5, meta={"lang": "en" if i % 2 else "de", "tags": ["a", f"t{i}"]})
        await store.replace_document(info, doc(f"d{i}"), cs, [unit_vec(rng) for _ in cs])
    q = unit_vec(rng)
    one = await store.search(info, q, 10, {"document_id": "d2"})
    assert len(one) == 5 and {h.chunk.document_id for h in one} == {"d2"}
    two = await store.search(info, q, 20, {"document_id": ["d1", "d3"]})
    assert {h.chunk.document_id for h in two} == {"d1", "d3"}
    en = await store.search(info, q, 20, {"lang": "en"})
    assert len(en) == 10 and all(h.chunk.metadata["lang"] == "en" for h in en)
    tagged = await store.search(info, q, 20, {"tags": ["a", "t0"]})
    assert {h.chunk.document_id for h in tagged} == {"d0"}
    allhits = await store.search(info, q, 20, {})
    assert [h.score for h in allhits] == sorted((h.score for h in allhits), reverse=True)
    assert all(-1.0 <= h.score <= 1.0 and h.scores == {"dense": h.score} for h in allhits)
    assert allhits[0].chunk.metadata["source"] == "s"
    stats = await store.stats(info)
    assert stats[0]["documents"] == 4 and stats[0]["chunks"] == 20


async def test_alias_on_another_backend_is_refused(db, store):
    from chatbot.core.settings import HnswSection
    from chatbot.stores.pgvector import PgVectorStore

    pg = PgVectorStore(db, HnswSection())
    await pg.ensure_index("docs", "m", DIMS, "v1")
    with pytest.raises(IndexMismatchError, match="pgvector store"):
        await store.get_index("docs")


async def test_forget_user_deletes_points_too(db, store, qclient):
    rng = random.Random(4)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    mine, theirs = chunks("d1", 3), chunks("d2", 2)
    await store.replace_document(info, doc("d1"), mine, [unit_vec(rng) for _ in mine], "u1")
    await store.replace_document(info, doc("d2"), theirs, [unit_vec(rng) for _ in theirs], "u2")
    assert await store.forget_user_documents("u1") == 3
    report = await PostgresMemoryStore(db).forget_user("u1")
    assert report.deleted["documents"] == 1
    hits = await store.search(info, unit_vec(rng), 50, {})
    assert {h.chunk.document_id for h in hits} == {"d2"}


async def test_drop_index_removes_the_collection(store, qclient):
    info = await store.create_index("docs", "m", DIMS, "v1", "docs_next")
    assert await qclient.collection_exists(info.name)
    with pytest.raises(ChatbotError, match="in use"):
        await store.drop_index(info.name)
    async with store.pool.connection() as conn:
        await conn.execute("DELETE FROM index_aliases WHERE alias = 'docs_next'")
    await store.drop_index(info.name)
    assert not await qclient.collection_exists(info.name)


# ---------------------------------------------------------------- end to end


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    path = tmp_path / "docs"
    shutil.copytree(ROOT / "samples" / "docs", path)
    return path


def settings_for(docs: Path, backend: str):
    return offline_settings(
        ingestion={"sources": [{"name": "samples", "type": "files", "path": str(docs)}]},
        vector_store={"backend": backend, "qdrant": {"location": ":memory:"}},
        retrieval={"strategy": "dense"},
    )


async def test_move_index_from_pgvector_to_qdrant_and_answer(db, qclient, docs):
    pg = container.build(settings_for(docs, "pgvector"), db)
    await pg.pipeline.sync_source(make_source(pg.settings, "samples"))
    await pg.pipeline.ingest_inline(
        "crm", [InlineDocument(id="1", text="Enterprise plans include SSO.")], user_id="u1"
    )
    q_settings = settings_for(docs, "qdrant")
    q = container.build(q_settings, db, store=QdrantStore(db, qclient, QdrantSection()))
    with pytest.raises(IndexMismatchError, match="chatbot reindex"):
        await q.retriever.retrieve("refund window", 3, {})

    sources = [make_source(q_settings, "samples")]
    report = await reindex(
        q.store, q.pipeline, sources, "docs_offline", backend="qdrant", activate=True
    )
    assert report.ready and report.activated and report.backend == "qdrant"
    assert report.passes[0].stored.indexed == ["crm:1"]

    resp = await q.orchestrator.run(
        ChatRequest(message="How long is the refund window for annual plans?")
    )
    assert resp.kind == "answer" and resp.citations
    assert resp.citations[0].document_id == "samples:refund-policy.md"

    # And back: rollback points the alias at the pgvector index again.
    await q.store.rollback("docs_offline")
    hits, _ = await pg.retriever.retrieve("refund window", 3, {})
    assert hits
