"""pgvector store against real Postgres: index metadata, atomic replace, filtered recall."""

import random

import psycopg
import pytest

from chatbot.core.errors import IndexMismatchError
from chatbot.core.settings import HnswSection
from chatbot.core.types import Chunk, ParsedDocument, Section
from chatbot.stores.pgvector import PgVectorStore

pytestmark = pytest.mark.integration

DIMS = 16


def unit_vec(rng: random.Random) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(DIMS)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def doc(doc_id: str, source: str = "s", h: str = "h1") -> ParsedDocument:
    return ParsedDocument(
        id=doc_id,
        source=source,
        uri=f"file://{doc_id}",
        title=doc_id,
        sections=[Section(text="x")],
        content_hash=h,
    )


def chunks(doc_id: str, n: int, prefix: str = "", meta: dict | None = None) -> list[Chunk]:
    return [
        Chunk(
            id=f"{doc_id}-{prefix}{i}",
            document_id=doc_id,
            position=i,
            text=f"{prefix}{doc_id} {i}",
            metadata=dict(meta or {}),
            content_hash=f"{prefix}{i}",
        )
        for i in range(n)
    ]


async def test_index_records_model_and_refuses_mismatch(db):
    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "model-a", DIMS, "v1")
    assert info.name == "docs__model_a__16" and info.text_config == "english"
    again = await store.ensure_index("docs", "model-a", DIMS, "v1")
    assert again.id == info.id
    with pytest.raises(IndexMismatchError):
        await store.ensure_index("docs", "model-b", DIMS, "v1")
    with pytest.raises(IndexMismatchError):
        await store.search(info, [0.1] * (DIMS + 1), 3, {})


async def test_replace_is_atomic_and_leaves_no_stale_chunks(db):
    rng = random.Random(1)
    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    old = chunks("d1", 5, "old")
    await store.replace_document(info, doc("d1", h="h1"), old, [unit_vec(rng) for _ in old])
    new = chunks("d1", 3, "new")
    await store.replace_document(info, doc("d1", h="h2"), new, [unit_vec(rng) for _ in new])
    hits = await store.search(info, unit_vec(rng), 50, {})
    assert sorted(h.chunk.id for h in hits) == sorted(c.id for c in new)
    state = await store.document_state(info, "d1")
    assert state["content_hash"] == "h2"


async def test_failed_replace_keeps_the_old_version(db):
    rng = random.Random(2)
    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    old = chunks("d1", 3, "old")
    await store.replace_document(info, doc("d1"), old, [unit_vec(rng) for _ in old])
    bad = chunks("d1", 2, "new")
    bad[1] = bad[1].model_copy(update={"id": bad[0].id})  # duplicate primary key -> fails mid-way
    with pytest.raises(psycopg.errors.UniqueViolation):
        await store.replace_document(info, doc("d1", h="h2"), bad, [unit_vec(rng) for _ in bad])
    hits = await store.search(info, unit_vec(rng), 50, {})
    assert sorted(h.chunk.id for h in hits) == sorted(c.id for c in old)
    assert (await store.document_state(info, "d1"))["content_hash"] == "h1"


async def seed(store, info, rng, n_docs=60, per_doc=50, rare_every=30):
    """3000 chunks; every 30th document is tagged rare (2 docs x 50 = 100 chunks, 3%)."""
    for d in range(n_docs):
        meta = {"tier": "rare" if d % rare_every == 0 else "common"}
        cs = chunks(f"doc{d}", per_doc, meta=meta)
        await store.replace_document(info, doc(f"doc{d}"), cs, [unit_vec(rng) for _ in cs])


@pytest.fixture
async def hnsw_pool(db, pg_url):
    """A pool whose planner may not seq-scan, so small test tables still use HNSW the way
    large production tables do."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        pg_url,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "options": "-c enable_seqscan=off -c enable_bitmapscan=off",
        },
    )
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def test_selective_filter_still_returns_top_k_with_hnsw(db, hnsw_pool):
    """ADR 012: HNSW filters after the graph search, so a selective filter can return fewer
    than k rows. Iterative scan keeps searching until k rows pass the filter."""
    rng = random.Random(3)
    k = 10
    store = PgVectorStore(hnsw_pool, HnswSection(ef_search=20), exact_scan_threshold=0)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    await seed(store, info, rng)
    query = unit_vec(rng)

    # The store's own WHERE clause, so a change to the filter SQL is checked against HNSW.
    where, params = PgVectorStore._where(info, {"tier": "rare"})
    async with hnsw_pool.connection() as conn:
        cur = await conn.execute(
            f"""EXPLAIN SELECT id FROM chunks c WHERE {where}
                ORDER BY (c.embedding::vector({DIMS})) <=> %s::vector({DIMS}) LIMIT {k}""",
            [*params, "[" + ",".join(map(str, query)) + "]"],
        )
        plan = "\n".join(r["QUERY PLAN"] for r in await cur.fetchall())
    assert f"chunks_hnsw_{info.id}" in plan

    hits = await store.search(info, query, k, {"tier": "rare"})
    assert len(hits) == k
    assert all(h.chunk.metadata["tier"] == "rare" for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    # The same query with iterative scan off under-returns: the failure this guards against.
    off = PgVectorStore(
        hnsw_pool, HnswSection(ef_search=20, iterative_scan="off"), exact_scan_threshold=0
    )
    assert len(await off.search(info, query, k, {"tier": "rare"})) < k


async def test_selective_filter_uses_exact_scan_and_matches_brute_force(db):
    rng = random.Random(4)
    store = PgVectorStore(db, HnswSection(), exact_scan_threshold=200)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    await seed(store, info, rng)
    query = unit_vec(rng)
    hits = await store.search(info, query, 5, {"document_id": "doc7"})
    assert len(hits) == 5 and {h.chunk.document_id for h in hits} == {"doc7"}
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM chunks WHERE document_id = 'doc7' "
            "ORDER BY embedding <=> %s::vector LIMIT 5",
            ("[" + ",".join(map(str, query)) + "]",),
        )
        expected = [r["id"] for r in await cur.fetchall()]
    assert [h.chunk.id for h in hits] == expected


async def test_delete_and_list(db):
    rng = random.Random(5)
    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    for d in ("a", "b"):
        cs = chunks(d, 2)
        await store.replace_document(info, doc(d), cs, [unit_vec(rng) for _ in cs])
    assert await store.list_document_ids(info, "s") == ["a", "b"]
    assert await store.delete_document(info, "a") == 1
    assert await store.list_document_ids(info, "s") == ["b"]
    hits = await store.search(info, unit_vec(rng), 10, {})
    assert {h.chunk.document_id for h in hits} == {"b"}


async def test_keyword_search_scores_bm25_and_follows_replace(db):
    """ADR 020: BM25 in SQL. Exact terms win, filters apply, re-ingest updates the index."""
    rng = random.Random(7)
    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "model-a", DIMS, "v1")
    texts = {
        "a": ["Error E309 means the device clock is wrong.", "Notes sync every minute."],
        "b": ["Refunds take 5 to 10 business days.", "The clock icon shows history."],
    }
    for doc_id, parts in texts.items():
        cs = [
            Chunk(
                id=f"{doc_id}-{i}",
                document_id=doc_id,
                position=i,
                text=t,
                metadata={"lang": "en"},
                content_hash=str(i),
            )
            for i, t in enumerate(parts)
        ]
        await store.replace_document(info, doc(doc_id), cs, [unit_vec(rng) for _ in cs])

    hits = await store.keyword_search(info, "what does error E309 mean", 5, {})
    assert hits[0].chunk.id == "a-0" and hits[0].retriever == "bm25"
    assert hits[0].scores["bm25"] == hits[0].score > 0
    # "clock" appears in two chunks, "E309" in one: the rarer term carries more weight.
    clock = await store.keyword_search(info, "clock", 5, {})
    assert {h.chunk.id for h in clock} == {"a-0", "b-1"}
    assert await store.keyword_search(info, "the of and", 5, {}) == []  # stopwords only
    assert [
        h.chunk.id for h in await store.keyword_search(info, "clock", 5, {"document_id": "b"})
    ] == ["b-1"]

    # After a re-ingest the old text is gone from keyword search too (ADR 013).
    new = [
        Chunk(
            id="a-x",
            document_id="a",
            position=0,
            text="Error E401 means expired.",
            content_hash="x",
        )
    ]
    await store.replace_document(info, doc("a", h="h2"), new, [unit_vec(rng)])
    assert await store.keyword_search(info, "E309", 5, {}) == []
    assert (await store.keyword_search(info, "E401", 5, {}))[0].chunk.id == "a-x"


async def test_calibrations_are_stored_per_scorer(db):
    from chatbot.core.types import Calibration

    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("docs", "model-a", DIMS, "v1")
    assert await store.get_calibration(info, "dense|dense") is None
    await store.save_calibration(
        info, Calibration(scorer="dense|dense", min_score=0.4, min_hits=2, metrics={"a": 1})
    )
    await store.save_calibration(
        info, Calibration(scorer="rerank|rerank[m](dense)", min_score=0.2, min_hits=1)
    )
    dense = await store.get_calibration(info, "dense|dense")
    assert dense.min_score == 0.4 and dense.min_hits == 2 and dense.metrics == {"a": 1}
    assert dense.calibrated_at is not None
    await store.save_calibration(info, Calibration(scorer="dense|dense", min_score=0.5, min_hits=1))
    assert (await store.get_calibration(info, "dense|dense")).min_score == 0.5
    assert [c.scorer for c in await store.list_calibrations(info)] == [
        "dense|dense",
        "rerank|rerank[m](dense)",
    ]


async def test_unalias_removes_the_alias_and_keeps_the_index(db):
    """Retiring a test index used to need SQL: drop refuses an index an alias points at."""
    from chatbot.core.errors import ChatbotError

    store = PgVectorStore(db, HnswSection())
    info = await store.ensure_index("scratch", "m", DIMS, "v1")
    with pytest.raises(ChatbotError, match="in use by alias scratch"):
        await store.drop_index(info.name)
    assert await store.remove_alias("scratch") == [info.name]
    listed = {s.name: s for s in await store.list_indexes()}
    assert listed[info.name].aliases == []  # the index is still there
    assert await store.get_index("scratch") is None
    await store.drop_index(info.name)  # and can now be dropped
    with pytest.raises(ChatbotError, match="No alias named 'scratch'"):
        await store.remove_alias("scratch")
