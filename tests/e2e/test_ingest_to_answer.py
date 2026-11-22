"""The milestone 1 path end to end: API -> Postgres job -> worker -> pgvector -> chat.

Runs with the offline profile (hashing embedder, extractive fake LLM), so it needs only
Postgres. tests/e2e/test_live_openai.py runs the same path against the real API.
"""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.core import container
from chatbot.ingestion.jobs.handlers import make_handlers
from chatbot.ingestion.jobs.worker import Worker
from tests.conftest import ROOT, offline_settings
from tests.unit.test_api import parse_sse

pytestmark = pytest.mark.e2e


@pytest.fixture
def app_client(db, tmp_path: Path):
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "samples" / "docs", docs)
    settings = offline_settings(
        ingestion={"sources": [{"name": "samples", "type": "files", "path": str(docs)}]}
    )
    comps = container.build(settings, db)
    with TestClient(create_app(settings, components=comps)) as client:
        client.components = comps
        client.docs = docs
        yield client


def run_jobs(client) -> None:
    c = client.components
    client.portal.call(Worker(c.queue, make_handlers(c)).drain)


def ingest(client) -> dict:
    resp = client.post("/v1/ingest", json={"source": "samples"})
    assert resp.status_code == 202
    run_jobs(client)
    job = client.get(resp.json()["status_url"]).json()
    assert job["status"] == "succeeded", job
    return job["result"]


def test_ingest_then_ask_returns_cited_answer(app_client):
    result = ingest(app_client)
    assert len(result["indexed"]) == 8 and result["failed"] == {}

    resp = app_client.post(
        "/v1/chat", json={"message": "How long is the refund window for annual plans?"}
    )
    body = resp.json()
    assert body["kind"] == "answer"
    assert body["citations"][0]["document_id"] == "samples:refund-policy.md"
    assert "30 days" in body["answer"]

    sources = app_client.get("/v1/sources").json()
    assert sources["sources"][0]["documents"] == 8
    assert sources["index"]["calibrated"] is False


def test_second_sync_skips_unchanged_documents(app_client):
    ingest(app_client)
    again = ingest(app_client)
    assert again["indexed"] == [] and len(again["unchanged"]) == 8


def test_edited_document_leaves_no_stale_chunk(app_client):
    """ADR 013: after an edit, the old text can no longer be retrieved or cited."""
    ingest(app_client)
    path = app_client.docs / "refund-policy.md"
    path.write_text(path.read_text().replace("30 days", "14 days").replace("30-day", "14-day"))
    result = ingest(app_client)
    assert result["indexed"] == ["samples:refund-policy.md"]

    body = app_client.post(
        "/v1/chat", json={"message": "How long is the refund window for annual plans?"}
    ).json()
    assert "14 days" in body["answer"] and "30 days" not in body["answer"]

    async def all_chunk_text():
        async with app_client.components.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT text FROM chunks WHERE document_id = 'samples:refund-policy.md'"
            )
            return " ".join(r["text"] for r in await cur.fetchall())

    assert "30 days" not in app_client.portal.call(all_chunk_text)


def test_document_deleted_at_source_is_removed(app_client):
    ingest(app_client)
    (app_client.docs / "refund-policy.md").unlink()
    result = ingest(app_client)
    assert result["removed"] == ["samples:refund-policy.md"]
    body = app_client.post(
        "/v1/chat", json={"message": "How long is the refund window for annual plans?"}
    ).json()
    assert all(c["document_id"] != "samples:refund-policy.md" for c in body["citations"])


def test_off_topic_question_gets_clarifying_question(app_client):
    ingest(app_client)
    body = app_client.post("/v1/chat", json={"message": "zebra quantum harmonica lessons"}).json()
    assert body["kind"] == "clarify" and body["citations"] == []


def test_streaming_ends_with_one_terminal_event(app_client):
    ingest(app_client)
    resp = app_client.post(
        "/v1/chat",
        json={"message": "What does sync error E309 mean?", "stream": True, "user_id": "u1"},
    )
    events = parse_sse(resp.text)
    assert events[-1][0] == "final"
    assert sum(name != "delta" for name, _ in events) == 1
    drafted = "".join(d["text"] for name, d in events if name == "delta")
    assert drafted.strip() == events[-1][1]["answer"]


def test_conversation_is_persisted_redacted_and_forgettable(app_client):
    ingest(app_client)
    first = app_client.post(
        "/v1/chat",
        json={
            "message": "I'm sam@example.com. Where are notes stored by default?",
            "user_id": "u7",
        },
    ).json()
    app_client.post(
        "/v1/chat",
        json={
            "message": "And in the US?",
            "user_id": "u7",
            "conversation_id": first["conversation_id"],
        },
    )

    async def turns():
        async with app_client.components.pool.connection() as conn:
            cur = await conn.execute("SELECT content FROM turns WHERE user_id = 'u7' ORDER BY id")
            return [r["content"] for r in await cur.fetchall()]

    stored = app_client.portal.call(turns)
    assert len(stored) == 4
    assert "sam@example.com" not in json.dumps(stored) and "[EMAIL]" in stored[0]

    # Long-term memory: the worker extracts facts in the background (ADR 016).
    app_client.post(
        "/v1/chat",
        json={
            "message": "I use the Team plan. Can I share a notebook?",
            "user_id": "u7",
            "conversation_id": first["conversation_id"],
        },
    )
    run_jobs(app_client)
    facts = app_client.get("/v1/users/u7/memory").json()["facts"]
    assert [f["fact"] for f in facts] == ["The user said: I use the Team plan."]

    async def rows_for_u7():
        async with app_client.components.pool.connection() as conn:
            counts = {}
            for table in ("user_facts", "conversation_summaries", "turns", "conversations"):
                cur = await conn.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id = 'u7'")
                counts[table] = (await cur.fetchone())["n"]
            return counts

    assert app_client.portal.call(rows_for_u7)["conversation_summaries"] == 1

    report = app_client.delete("/v1/users/u7/memory").json()
    assert report["deleted"]["turns"] == 6 and report["deleted"]["user_facts"] == 1
    assert app_client.portal.call(turns) == []
    assert set(app_client.portal.call(rows_for_u7).values()) == {0}


def test_golden_retrieval_hit_rate(app_client):
    """Expected document in the top 3 for answerable golden questions (offline embedder)."""
    ingest(app_client)
    retriever = app_client.components.retriever
    golden = [
        json.loads(line) for line in (ROOT / "evals/datasets/golden.jsonl").read_text().splitlines()
    ]
    answerable = [g for g in golden if g["answerable"]]

    async def hit(q):
        hits, _ = await retriever.retrieve(q["question"], 3, {})
        return any(h.chunk.document_id in q["expected_documents"] for h in hits)

    rate = sum(app_client.portal.call(hit, q) for q in answerable) / len(answerable)
    assert rate >= 0.9, f"hit@3 = {rate:.2f}"


def test_calibrate_then_eval_with_hybrid_search(db, tmp_path: Path):
    """Milestone 3 on Postgres: BM25 in SQL fused with dense, calibrated, then evaluated."""
    from chatbot.evals.commands import calibrate, evaluate

    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "samples" / "docs", docs)
    settings = offline_settings(
        ingestion={"sources": [{"name": "samples", "type": "files", "path": str(docs)}]},
        retrieval={"strategy": "hybrid"},
    )
    comps = container.build(settings, db)
    with TestClient(create_app(settings, components=comps)) as client:
        client.components = comps
        ingest(client)
        assert client.get("/v1/sources").json()["index"]["calibrated"] is False

        result, calibration = client.portal.call(lambda: calibrate(comps, apply=True))
        assert calibration.scorer == "dense|hybrid(dense,bm25)"
        assert result.met_target
        index = client.get("/v1/sources").json()["index"]
        assert index["calibrated"] is True and index["scorer"] == calibration.scorer
        assert index["calibration"]["min_hits"] == calibration.min_hits

        report = client.portal.call(lambda: evaluate(comps))
        assert report.meta["calibrated"] is True
        assert report.summary["retrieval.hit@3"] >= 0.95
        assert report.summary["errors"] == 0
