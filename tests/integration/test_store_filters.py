"""Metadata filters mean the same thing in every vector store (``metadata_matches``).

The same documents and filters run against the in-memory store, pgvector (exact scan and
HNSW) and Qdrant. Before this test, a scalar filter on a list field matched in Qdrant, never
in pgvector, and only by equality in the in-memory store.
"""

import os
import random

import pytest

from chatbot.core.interfaces import metadata_matches
from chatbot.core.settings import HnswSection, QdrantSection
from chatbot.stores.pgvector import PgVectorStore
from chatbot.testing.fakes import InMemoryVectorStore
from tests.integration.test_pgvector import DIMS, chunks, doc, unit_vec

pytestmark = pytest.mark.integration

DOCS = {
    "d1": {"tags": ["hours", "contact"], "team": "support"},
    "d2": {"tags": ["billing"], "team": "sales"},
    "d3": {"tags": "contact", "team": "support"},  # a scalar where others have lists
    "d4": {"team": 7},  # no tags at all; a number, not a string
}

CASES = [
    ({"tags": "contact"}, {"d1", "d3"}),  # scalar: equal, or an element of a list
    ({"tags": ["contact"]}, {"d1", "d3"}),
    ({"tags": ["hours", "contact"]}, {"d1"}),  # list: every element
    ({"team": "support"}, {"d1", "d3"}),
    ({"team": "support", "tags": "hours"}, {"d1"}),  # keys are ANDed
    ({"tags": "nope"}, set()),
    ({"team": 7}, {"d4"}),
    ({"team": "7"}, set()),  # types are not coerced
    ({"document_id": ["d1", "d2"], "team": "sales"}, {"d2"}),
]


async def check_filters(store) -> None:
    rng = random.Random(5)
    info = await store.ensure_index("docs", "m", DIMS, "v1")
    for doc_id, meta in DOCS.items():
        cs = chunks(doc_id, 2, meta=meta)
        await store.replace_document(info, doc(doc_id), cs, [unit_vec(rng) for _ in cs])
    query = unit_vec(rng)
    for filters, expected in CASES:
        hits = await store.search(info, query, 20, filters)
        assert {h.chunk.document_id for h in hits} == expected, filters
        # The reference rule agrees, so a custom store can be checked against it too.
        assert {d for d, m in DOCS.items() if _ref(d, m, filters)} == expected, filters


async def test_filters_in_memory_store():
    await check_filters(InMemoryVectorStore())


@pytest.mark.parametrize("exact_scan_threshold", [2000, 0], ids=["exact-scan", "hnsw"])
async def test_filters_in_pgvector(db, exact_scan_threshold):
    await check_filters(PgVectorStore(db, HnswSection(), exact_scan_threshold=exact_scan_threshold))


@pytest.mark.qdrant
async def test_filters_in_qdrant(db):
    pytest.importorskip("qdrant_client", reason="uv sync --extra qdrant")
    from qdrant_client import AsyncQdrantClient

    from chatbot.stores.qdrant import QdrantStore

    url = os.environ.get("QDRANT_TEST_URL")
    client = AsyncQdrantClient(url=url) if url else AsyncQdrantClient(location=":memory:")
    try:
        for c in (await client.get_collections()).collections:
            await client.delete_collection(c.name)
        await check_filters(QdrantStore(db, client, QdrantSection()))
    finally:
        await client.close()


def _ref(doc_id: str, meta: dict, filters: dict) -> bool:
    wanted = filters.get("document_id")
    if wanted is not None and doc_id not in (wanted if isinstance(wanted, list) else [wanted]):
        return False
    return metadata_matches(meta, filters)
