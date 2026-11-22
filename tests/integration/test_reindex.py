"""Re-indexing against Postgres (ADR 023): build beside the live index, switch, roll back."""

import shutil
from pathlib import Path

import pytest

from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.errors import ChatbotError, IndexMismatchError
from chatbot.ingestion.pipeline import InlineDocument
from chatbot.ingestion.reindex import reindex
from chatbot.providers.embeddings.hashing import HashingEmbedder
from tests.conftest import ROOT, offline_settings

pytestmark = pytest.mark.integration

QUESTION = "How long is the refund window for annual plans?"


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    path = tmp_path / "docs"
    shutil.copytree(ROOT / "samples" / "docs", path)
    return path


def settings_for(docs: Path, **overrides):
    return offline_settings(
        ingestion={
            "sources": [{"name": "samples", "type": "files", "path": str(docs)}],
            **overrides.pop("ingestion", {}),
        },
        **overrides,
    )


def build(db, settings, embedder=None):
    parts = {"embedder": embedder} if embedder is not None else {}
    return container.build(settings, db, **parts)


async def seed(c) -> None:
    """The live index: the sample files plus two documents pushed through the API."""
    await c.pipeline.sync_source(make_source(c.settings, "samples"))
    await c.pipeline.ingest_inline(
        "crm",
        [
            InlineDocument(id="1", text="Enterprise customers get a named account manager."),
            InlineDocument(id="2", text="Invoices are sent on the first business day."),
        ],
        user_id="u1",
    )


async def run(c, **kw):
    sources = [make_source(c.settings, s.name) for s in c.settings.ingestion.sources]
    return await reindex(
        c.store, c.pipeline, sources, c.settings.vector_store.index, backend="pgvector", **kw
    )


async def test_new_embedder_builds_beside_live_index_then_switches(db, docs):
    old = build(db, settings_for(docs))
    await seed(old)
    live = await old.store.get_index("docs_offline")
    hits_before, _ = await old.retriever.retrieve(QUESTION, 3, {})
    assert hits_before

    new = build(db, settings_for(docs), HashingEmbedder(256, "hashing-v2"))
    with pytest.raises(IndexMismatchError, match="chatbot reindex"):
        await new.retriever.retrieve(QUESTION, 3, {})  # never search another model's index

    report = await run(new)
    assert report.ready and not report.activated and not report.resumed
    assert report.old_index == live.name and report.new_index != live.name
    assert report.embedding_model == "hashing-v2" and report.dims == 256
    first = report.passes[0]
    assert len(first.sources["samples"].indexed) == 8
    assert sorted(first.stored.indexed) == ["crm:1", "crm:2"]
    # The catch-up pass found nothing new.
    assert report.summary()["changed_in_last_pass"] == 0

    # The alias still serves the old index; the new one is reachable as docs_offline_next.
    assert (await old.store.get_index("docs_offline")).name == live.name
    staged = await new.store.get_index("docs_offline_next")
    assert staged.embedding_model == "hashing-v2"
    owners = [s.user_id async for s in new.store.iter_documents(staged, {"samples"})]
    assert owners == ["u1", "u1"]  # ownership carried over, so forget_user still finds them

    info = await new.store.activate("docs_offline", "docs_offline_next")
    assert info.name == report.new_index
    hits, _ = await new.retriever.retrieve(QUESTION, 3, {})
    assert hits and hits[0].chunk.document_id == hits_before[0].chunk.document_id
    with pytest.raises(IndexMismatchError):
        await old.retriever.retrieve(QUESTION, 3, {})  # old config now refused
    assert await new.store.get_index("docs_offline_next") is None

    listed = {s.name: s for s in await new.store.list_indexes()}
    assert listed[report.new_index].aliases == ["docs_offline"]
    assert listed[live.name].previous_of == ["docs_offline"]
    assert listed[report.new_index].status == "ready"
    assert listed[report.new_index].documents == 10

    rolled = await new.store.rollback("docs_offline")
    assert rolled.name == live.name
    hits, _ = await old.retriever.retrieve(QUESTION, 3, {})
    assert hits
    with pytest.raises(ChatbotError, match="in use"):
        await new.store.drop_index(live.name)
    await new.store.rollback("docs_offline")  # forward again
    await new.store.drop_index(live.name)
    assert live.name not in {s.name for s in await new.store.list_indexes()}
    with pytest.raises(ChatbotError, match="no previous index"):
        await new.store.rollback("docs_offline")


async def test_chunker_change_reindexes_with_same_model(db, docs):
    old = build(db, settings_for(docs))
    await seed(old)
    smaller = settings_for(docs, ingestion={"chunker": {"size_tokens": 128, "overlap_tokens": 16}})
    new = build(db, smaller)
    report = await run(new, activate=True)
    assert report.activated
    info = await new.store.get_index("docs_offline")
    assert info.chunker_version == new.chunker.version != old.chunker.version
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT chunker_version FROM documents WHERE index_id = %s", (info.id,)
        )
        assert [r["chunker_version"] for r in await cur.fetchall()] == [new.chunker.version]


async def test_resume_and_catch_up(db, docs):
    old = build(db, settings_for(docs))
    await seed(old)
    new = build(db, settings_for(docs), HashingEmbedder(256, "hashing-v2"))
    first = await run(new)
    assert first.ready

    # The live index changes after the build: one API document deleted, one added.
    live = await old.store.get_index("docs_offline")
    await old.store.delete_document(live, "crm:2")
    await old.pipeline.ingest_inline("crm", [InlineDocument(id="3", text="Support is 24/7.")])
    (docs / "refund-policy.md").unlink()

    again = await run(new)
    assert again.resumed and again.new_index == first.new_index
    stored = again.passes[0].stored
    assert stored.indexed == ["crm:3"] and stored.removed == ["crm:2"]
    assert again.passes[0].sources["samples"].removed == ["samples:refund-policy.md"]
    assert len(again.passes[0].sources["samples"].unchanged) == 7  # nothing re-embedded

    other = build(db, settings_for(docs), HashingEmbedder(128, "hashing-v3"))
    with pytest.raises(ChatbotError, match="--restart"):
        await run(other)
    restarted = await run(other, restart=True)
    assert restarted.new_index != first.new_index and restarted.ready


async def test_documents_without_stored_text_block_the_switch(db, docs):
    old = build(db, settings_for(docs))
    await seed(old)
    async with db.connection() as conn:  # as if written before migration 0004
        await conn.execute("UPDATE documents SET sections = NULL WHERE source = 'crm'")
    new = build(db, settings_for(docs), HashingEmbedder(256, "hashing-v2"))
    report = await run(new)
    assert not report.ready and report.missing == ["crm:1", "crm:2"]
    with pytest.raises(ChatbotError, match="still building"):
        await new.store.activate("docs_offline", "docs_offline_next")
    listed = {s.name: s for s in await new.store.list_indexes()}
    assert listed[report.new_index].status == "building"
    assert listed[report.new_index].build_report["missing"] == ["crm:1", "crm:2"]

    allowed = await run(new, allow_missing=True, activate=True)
    assert allowed.resumed and allowed.ready and allowed.activated


async def test_ingest_into_staging_alias_while_building(db, docs):
    """Documents pushed to <alias>_next (e.g. to fill gaps) land in the new index only."""
    old = build(db, settings_for(docs))
    await seed(old)
    new = build(db, settings_for(docs), HashingEmbedder(256, "hashing-v2"))
    await run(new)
    staging = build(
        db,
        settings_for(docs, vector_store={"index": "docs_offline_next"}),
        HashingEmbedder(256, "hashing-v2"),
    )
    await staging.pipeline.ingest_inline("crm", [InlineDocument(id="9", text="Late document.")])
    staged = await staging.store.get_index("docs_offline_next")
    assert "crm:9" in await staging.store.list_document_ids(staged, "crm")
    live = await old.store.get_index("docs_offline")
    assert "crm:9" not in await old.store.list_document_ids(live, "crm")
