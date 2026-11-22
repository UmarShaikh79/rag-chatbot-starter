"""Token and cost accounting (ADR 028): the ledger, usage records, reports and the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.settings import Settings
from chatbot.core.types import ChatRequest
from chatbot.observability.accounting import InMemoryUsageStore
from chatbot.observability.instrument import MeteredLLM, metered_operation, unwrap
from chatbot.observability.usage import UsageCall, UsageRecord, metering
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore
from tests.conftest import offline_settings

QUESTION = "How long is the refund window for annual plans?"


def priced_settings(**overrides) -> Settings:
    """The offline profile with prices on the fake model and the hashing embedder."""
    s = offline_settings(**overrides)
    data = s.model_dump()
    data["catalog"]["llm"]["fake:extractive"]["price_per_mtok"] = {"input": 1.0, "output": 4.0}
    data["catalog"]["embeddings"]["hashing:384"]["price_per_mtok"] = {"input": 0.02}
    return Settings.model_validate(data)


async def build(settings: Settings, store=None):
    c = container.build(
        settings,
        pool=None,
        answer_llm=FakeLLM(),
        cheap_llm=FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
        usage_store=store,
    )
    await c.pipeline.sync_source(make_source(settings, "samples"))
    return c


async def test_models_are_wrapped_once_and_keep_their_interface():
    c = await build(offline_settings())
    assert isinstance(c.answer_llm, MeteredLLM)
    assert isinstance(unwrap(c.answer_llm), FakeLLM)
    assert c.answer_llm.model_id == "fake-extractive"
    assert c.embedder.dims == 384 and c.embedder.model_id == "hashing-v1"
    assert c.answer_llm.calls is unwrap(c.answer_llm).calls  # other attributes pass through
    assert container.metered_llm(c.settings, "answer", c.answer_llm) is c.answer_llm


async def test_turn_record_matches_the_response_usage_and_cost():
    store = InMemoryUsageStore()
    c = await build(priced_settings(), store)
    resp = await c.orchestrator.run(ChatRequest(message=QUESTION, user_id="u-1"))
    [rec] = store.records
    assert rec.operation == "chat"
    assert rec.user_id == "u-1" and rec.conversation_id == resp.conversation_id
    assert rec.trace_id == resp.trace_id
    assert rec.usage.prompt_tokens == resp.usage.prompt_tokens > 0
    assert rec.usage.completion_tokens == resp.usage.completion_tokens
    assert rec.usage.embedding_tokens == resp.usage.embedding_tokens > 0
    assert rec.usage.cost_usd == pytest.approx(resp.usage.cost_usd)
    assert rec.unpriced_calls == 0
    purposes = {(call.kind, call.purpose) for call in rec.calls}
    assert ("chat", "answer") in purposes and ("embedding", "query") in purposes
    answer = next(call for call in rec.calls if call.purpose == "answer")
    expected = (answer.prompt_tokens * 1.0 + answer.completion_tokens * 4.0) / 1_000_000
    assert answer.cost_usd == pytest.approx(expected)


async def test_unpriced_models_are_counted_not_guessed():
    store = InMemoryUsageStore()
    c = await build(offline_settings(), store)  # models.yaml ships with null prices
    resp = await c.orchestrator.run(ChatRequest(message=QUESTION))
    [rec] = store.records
    assert resp.usage.cost_usd is None and rec.usage.cost_usd is None
    assert rec.unpriced_calls == len(rec.calls) > 0


async def test_streamed_turn_is_recorded_once():
    store = InMemoryUsageStore()
    c = await build(priced_settings(), store)
    events = [e async for e in c.orchestrator.stream(ChatRequest(message=QUESTION))]
    assert events[-1].terminal
    [rec] = store.records
    assert rec.usage.cost_usd == pytest.approx(events[-1].data["usage"]["cost_usd"])


async def test_abandoned_stream_still_records_what_was_spent():
    store = InMemoryUsageStore()
    c = await build(priced_settings(), store)
    stream = c.orchestrator.stream(ChatRequest(message=QUESTION))
    async for event in stream:
        if event.event == "delta":
            break  # the client went away mid-answer
    await stream.aclose()
    # Calls that finished are recorded. The answer call that was cut off is not: the
    # provider reports usage only at the end of a stream (ADR 028).
    [rec] = store.records
    assert any(call.purpose == "query" for call in rec.calls)


async def test_memory_refresh_in_the_worker_counts_too():
    store = InMemoryUsageStore()
    c = await build(priced_settings(memory={"run_in": "inline"}), store)
    for _ in range(3):
        await c.orchestrator.run(
            ChatRequest(message="I use the Android app. " + QUESTION, user_id="u-9")
        )
    purposes = {call.purpose for rec in store.records for call in rec.calls}
    assert "extract_facts" in purposes  # inline memory work lands in the turn's record


async def test_nested_operations_count_towards_the_outer_ledger():
    with metering() as outer:
        with metering() as inner:
            from chatbot.observability.usage import record_call

            record_call(
                UsageCall(kind="chat", model="m", role="cheap", purpose="x", prompt_tokens=3)
            )
        assert len(inner.calls) == 1
    assert outer.total().prompt_tokens == 3


async def test_metered_operation_records_even_when_the_work_fails():
    store = InMemoryUsageStore()
    c = await build(priced_settings(), store)
    with pytest.raises(RuntimeError):
        async with metered_operation("ingest:samples", store, user_id="u-2"):
            await c.embedder.embed_documents(["one", "two"])
            raise RuntimeError("source went away")
    [rec] = store.records
    assert rec.operation == "ingest:samples" and rec.user_id == "u-2"
    assert [call.purpose for call in rec.calls] == ["documents"]


async def test_report_groups_and_filters():
    store = InMemoryUsageStore()
    calls = [
        UsageCall(
            kind="chat",
            model="big",
            role="answer",
            purpose="answer",
            prompt_tokens=100,
            completion_tokens=10,
            cost_usd=0.5,
        ),
        UsageCall(
            kind="embedding",
            model="emb",
            role="embed",
            purpose="query",
            embedding_tokens=5,
            cost_usd=None,
        ),
    ]
    for user in ("a", "a", "b"):
        rec = UsageRecord(operation="chat", trace_id="t", user_id=user, calls=calls)
        with metering() as ledger:
            ledger.calls.extend(calls)
            rec = rec.model_copy(update={"usage": ledger.total(), "unpriced_calls": 1})
        await store.record(rec)

    by_user = await store.report("user")
    assert [(r.key, r.operations) for r in by_user.rows] == [("a", 2), ("b", 1)]
    assert by_user.total.cost_usd == pytest.approx(1.5)
    assert by_user.total.unpriced_calls == 3
    by_model = await store.report("model")
    rows = {r.key: r for r in by_model.rows}
    assert rows["big"].prompt_tokens == 300 and rows["big"].cost_usd == pytest.approx(1.5)
    assert rows["emb"].cost_usd is None and rows["emb"].unpriced_calls == 3
    only_b = await store.report("operation", user_id="b")
    assert only_b.total.operations == 1
    future = await store.report("day", since=datetime.now(UTC) + timedelta(days=1))
    assert future.rows == []
    assert await store.forget_user("a") == 2
    assert [r.key for r in (await store.report("user")).rows] == ["b", None]


def test_usage_api_is_admin_only_and_reports(fake_components):
    store = InMemoryUsageStore()
    fake_components.usage_store = store
    fake_components.orchestrator.usage_store = store
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as client:
        client.post("/v1/chat", json={"message": QUESTION, "user_id": "u-5"})
        resp = client.get("/v1/usage", params={"group_by": "user"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"][0]["key"] == "u-5"
    assert body["total"]["operations"] == 1


def test_usage_api_says_when_records_are_off(fake_components):
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as client:
        resp = client.get("/v1/usage")
    assert resp.status_code == 404 and "persist" in resp.json()["detail"]
