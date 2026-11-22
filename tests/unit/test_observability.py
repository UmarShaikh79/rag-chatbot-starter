"""Tracing and metrics (ADR 026) and the Langfuse export path (ADR 027), with in-memory
OpenTelemetry exporters: no collector, no network."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("opentelemetry.sdk", reason="uv sync --extra otel")

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from chatbot.api.app import create_app
from chatbot.core.types import ChatRequest
from chatbot.ingestion.jobs.handlers import SYNC_SOURCE, make_handlers
from chatbot.ingestion.jobs.worker import Worker
from chatbot.observability import tracing
from chatbot.observability.accounting import InMemoryUsageStore
from chatbot.observability.tracing import (
    INPUT_ATTR,
    OUTPUT_ATTR,
    ROOT_ATTR,
    SESSION_ATTR,
    USER_ATTR,
    configure_telemetry,
    langfuse_endpoint,
    shutdown_telemetry,
)
from tests.conftest import offline_settings
from tests.unit.test_orchestrator import build

QUESTION = "How long is the refund window for annual plans?"


class Otel:
    """Telemetry on, exporting to memory (a fresh exporter and reader per install, since
    shutting a provider down stops its exporter)."""

    def install(self, **obs):
        self.exporter, self.reader = InMemorySpanExporter(), InMemoryMetricReader()
        settings = offline_settings(observability={"otel": {"enabled": True, **obs}})
        tel = configure_telemetry(
            settings, "test", span_exporters=[self.exporter], metric_reader=self.reader
        )
        assert tel.enabled
        return self


@pytest.fixture
def otel():
    yield Otel().install()
    shutdown_telemetry()


def spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def metric_points(reader) -> dict[str, list]:
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out.setdefault(m.name, []).extend(m.data.data_points)
    return out


def test_disabled_by_default_is_a_noop():
    tel = configure_telemetry(offline_settings())
    assert not tel.enabled
    with tracing.span("anything", attrs={"x": 1}) as s:
        s.io(input="secret")  # ignored
        assert s.trace_id is None


async def test_chat_turn_is_one_trace_with_steps_and_a_generation(otel):
    exporter, reader = otel.exporter, otel.reader
    c = await build()
    exporter.clear()
    resp = await c.orchestrator.run(ChatRequest(message=QUESTION, user_id="u-7"))
    assert resp.kind == "answer"

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    for step in ("chat", "input guards", "load memory", "retrieve", "answer", "output guards"):
        assert step in names, names
    root = spans_by_name(exporter)["chat"]
    assert root.parent is None and root.attributes[ROOT_ATTR] is True
    assert root.attributes["chatbot.kind"] == "answer"
    # The response's trace_id is the OpenTelemetry trace ID: it finds the trace.
    assert format(root.context.trace_id, "032x") == resp.trace_id
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}

    gen = next(s for s in spans if s.attributes.get("langfuse.observation.type") == "generation")
    assert gen.name == "chat fake-extractive"
    assert gen.attributes["gen_ai.request.model"] == "fake-extractive"
    assert gen.attributes["chatbot.purpose"] == "answer"
    assert gen.attributes["gen_ai.usage.input_tokens"] > 0
    emb = next(s for s in spans if s.attributes.get("langfuse.observation.type") == "embedding")
    assert emb.attributes["chatbot.purpose"] == "query"

    # User and session are on every span (Langfuse filters need that).
    for s in spans:
        assert s.attributes[USER_ATTR] == "u-7"
    assert root.attributes[SESSION_ATTR] == resp.conversation_id
    # Text is off by default.
    assert not any(INPUT_ATTR in s.attributes or OUTPUT_ATTR in s.attributes for s in spans)

    points = metric_points(reader)
    assert sum(p.value for p in points["chatbot.chat.turns"]) == 1
    tokens = {p.attributes["gen_ai.token.type"] for p in points["gen_ai.client.token.usage"]}
    assert tokens == {"input", "output"}
    assert "chatbot.retrieval.requests" in points


async def test_captured_text_is_the_redacted_text(otel):
    exporter = otel.install(capture_text=True).exporter
    c = await build()
    exporter.clear()
    await c.orchestrator.run(ChatRequest(message="My email is jane.doe@example.com. " + QUESTION))
    root = spans_by_name(exporter)["chat"]
    assert "jane.doe@example.com" not in root.attributes[INPUT_ATTR]
    assert "refund" in root.attributes[INPUT_ATTR]
    assert "30 days" in root.attributes[OUTPUT_ATTR]
    for s in exporter.get_finished_spans():
        for key in (INPUT_ATTR, OUTPUT_ATTR):
            assert "jane.doe@example.com" not in str(s.attributes.get(key, ""))


async def test_streamed_turn_has_one_root_and_records_the_kind(otel):
    exporter = otel.exporter
    c = await build()
    exporter.clear()
    events = [e async for e in c.orchestrator.stream(ChatRequest(message=QUESTION))]
    assert events[-1].event in ("final", "replace")
    roots = [s for s in exporter.get_finished_spans() if s.attributes.get(ROOT_ATTR)]
    assert len(roots) == 1 and roots[0].attributes["chatbot.stream"] is True
    gen = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat fake"))
    assert "langfuse.observation.completion_start_time" in gen.attributes


async def test_failed_turn_marks_the_span_as_error(otel):
    exporter = otel.exporter
    c = await build()

    async def boom(*a, **k):
        raise RuntimeError("database down")

    c.orchestrator.retriever.retrieve = boom
    exporter.clear()
    events = [e async for e in c.orchestrator.stream(ChatRequest(message=QUESTION))]
    assert events[-1].event == "error"
    root = spans_by_name(exporter)["chat"]
    assert root.status.status_code.name == "ERROR"
    assert root.attributes["chatbot.kind"] == "error"
    retrieve = spans_by_name(exporter)["retrieve"]
    assert retrieve.status.status_code.name == "ERROR"


def test_api_server_span_continues_traceparent_and_skips_probes(otel, fake_components):
    exporter = otel.exporter
    trace_hex = "4bf92f3577b34da6a3ce929d0e0e4736"
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as client:
        exporter.clear()
        assert client.get("/livez").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert exporter.get_finished_spans() == ()  # probes are not traced
        resp = client.post(
            "/v1/chat",
            json={"message": QUESTION},
            headers={"traceparent": f"00-{trace_hex}-00f067aa0ba902b7-01"},
        )
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == trace_hex == resp.json()["trace_id"]
    server = spans_by_name(exporter)["POST /v1/chat"]
    assert server.attributes["http.route"] == "/v1/chat"
    assert server.attributes["http.response.status_code"] == 200
    chat = spans_by_name(exporter)["chat"]
    assert chat.parent.span_id == server.context.span_id


async def test_worker_job_is_a_root_span_and_a_usage_record(otel):
    exporter, reader = otel.exporter, otel.reader
    c = await build()
    store = InMemoryUsageStore()
    exporter.clear()
    await c.queue.enqueue(SYNC_SOURCE, {"source": "samples", "force": True})
    assert await Worker(c.queue, make_handlers(c), usage_store=store).drain() == 1
    job = spans_by_name(exporter)["job:sync_source"]
    assert job.attributes[ROOT_ATTR] is True
    assert any(s.name.startswith("embeddings ") for s in exporter.get_finished_spans())
    [rec] = store.records
    assert rec.operation == "job:sync_source" and rec.usage.embedding_tokens > 0
    assert rec.trace_id == format(job.context.trace_id, "032x")
    jobs = metric_points(reader)["chatbot.jobs"]
    assert jobs[0].attributes["chatbot.job.status"] == "succeeded"


# ---------------------------------------------------------------- export filtering


def _export_through(**kw):
    """Spans from the chatbot and the HTTP scopes, through a filtering exporter."""
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(tracing._filtering_exporter(inner, **kw)))
    tel = tracing.OtelTelemetry(provider, None, capture_text=True)
    with (
        tel.span("POST /v1/chat", server=True),
        tracing.trace_attributes("u-1", "conv-1"),
        tel.span("chat", root=True) as s,
    ):
        s.io(input="question", output="answer")
    return {s.name: s for s in inner.get_finished_spans()}


def test_langfuse_export_keeps_chatbot_spans_and_makes_the_turn_a_root():
    spans = _export_through(only_scope=tracing.SCOPE, strip=(), detach_roots=True)
    assert set(spans) == {"chat"}  # the HTTP span is not sent to Langfuse
    chat = spans["chat"]
    assert chat.parent is None
    assert chat.attributes[INPUT_ATTR] == "question"
    assert chat.attributes[USER_ATTR] == "u-1"
    assert chat.attributes["langfuse.trace.name"] == "chat"


def test_backend_without_capture_gets_no_text():
    spans = _export_through(only_scope=None, strip=tracing.TEXT_ATTRS, detach_roots=False)
    assert set(spans) == {"chat", "POST /v1/chat"}
    assert INPUT_ATTR not in spans["chat"].attributes
    assert spans["chat"].parent is not None  # unchanged for a normal OTel backend


def test_langfuse_endpoint_and_auth():
    settings = offline_settings(observability={"langfuse": {"enabled": True}})
    url, headers = langfuse_endpoint(
        settings,
        {
            "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com/",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
            "LANGFUSE_SECRET_KEY": "sk-lf-2",
        },
    )
    assert url == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    assert base64.b64decode(headers["Authorization"].split()[1]) == b"pk-lf-1:sk-lf-2"
    assert headers["x-langfuse-ingestion-version"] == "4"
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        langfuse_endpoint(settings, {})


def test_langfuse_on_builds_an_exporter_without_the_sdk():
    settings = offline_settings(observability={"langfuse": {"enabled": True}})
    tel = tracing.build_otel(
        settings,
        "test",
        environ={"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"},
    )
    try:
        assert tel.capture_text is True  # langfuse.capture_text defaults on
        assert tel.meter_provider is None  # Langfuse takes traces only
    finally:
        tel.shutdown()


@pytest.fixture
def global_otel(otel):
    """The test providers registered as OpenTelemetry's global ones, as production does
    (configure_telemetry skips that when given test exporters). FastAPI's own telemetry only
    switches on with a global provider, so without this the bug below cannot show."""
    import opentelemetry.metrics._internal as metrics_api
    from opentelemetry import trace
    from opentelemetry.util._once import Once

    saved = (
        trace._TRACER_PROVIDER,
        trace._TRACER_PROVIDER_SET_ONCE,
        metrics_api._METER_PROVIDER,
        metrics_api._METER_PROVIDER_SET_ONCE,
    )
    tel = tracing.telemetry()
    trace._TRACER_PROVIDER, trace._TRACER_PROVIDER_SET_ONCE = None, Once()
    metrics_api._METER_PROVIDER, metrics_api._METER_PROVIDER_SET_ONCE = None, Once()
    trace.set_tracer_provider(tel.tracer_provider)
    metrics_api.set_meter_provider(tel.meter_provider)
    yield otel
    (
        trace._TRACER_PROVIDER,
        trace._TRACER_PROVIDER_SET_ONCE,
        metrics_api._METER_PROVIDER,
        metrics_api._METER_PROVIDER_SET_ONCE,
    ) = saved


def test_fastapi_telemetry_skips_probes_and_counts_requests_once(global_otel, fake_components):
    """Regression: FastAPI 0.165+ traces and meters requests itself. It added an empty second
    root trace per request, made every health check a trace, and counted every request twice
    in http.server.request.duration."""
    from opentelemetry.trace import SpanKind

    exporter, reader = global_otel.exporter, global_otel.reader
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as client:
        exporter.clear()
        for probe in ("/healthz", "/livez", "/readyz"):
            assert client.get(probe).status_code == 200
        assert exporter.get_finished_spans() == ()
        assert client.post("/v1/chat", json={"message": QUESTION}).status_code == 200

    spans = exporter.get_finished_spans()
    servers = [s for s in spans if s.kind == SpanKind.SERVER]
    assert [s.name for s in servers] == ["POST /v1/chat"]  # one server span: the template's
    assert {s.context.trace_id for s in spans} == {servers[0].context.trace_id}  # one trace
    assert not any(s.instrumentation_scope.name == "fastapi" for s in spans)

    scopes, routes = set(), set()
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "http.server.request.duration":
                    scopes.add(sm.scope.name)
                    routes |= {p.attributes.get("http.route") for p in m.data.data_points}
    assert scopes == {"chatbot"}  # recorded once, by TraceMiddleware
    assert routes == {"/v1/chat"}  # probes are not metered


async def test_rerank_is_its_own_span_and_a_fallback_is_marked(otel):
    from chatbot.providers.rerankers import RerankError

    exporter = otel.exporter
    c = await build(retrieval={"rerank": {"enabled": True, "model": "lexical:overlap"}})
    exporter.clear()
    await c.orchestrator.run(ChatRequest(message=QUESTION))
    rerank = spans_by_name(exporter)["rerank"]
    assert rerank.attributes["chatbot.rerank.model"] == "rerank[lexical-v1]"
    attrs = rerank.attributes
    assert attrs["chatbot.rerank.candidates"] >= attrs["chatbot.rerank.top_n"]
    assert rerank.parent.span_id == spans_by_name(exporter)["retrieve"].context.span_id

    class Broken:
        signature = "rerank[broken]"

        async def rerank(self, query, hits, top_n):
            raise RerankError("HTTP 429")

    c.retriever.reranker = Broken()
    exporter.clear()
    resp = await c.orchestrator.run(ChatRequest(message=QUESTION))
    spans = spans_by_name(exporter)
    assert spans["rerank"].attributes["chatbot.rerank.fallback"] is True
    assert spans["rerank"].status.status_code.name == "ERROR"
    assert spans["retrieve"].attributes["chatbot.retrieval.scorer"] == "dense|dense"
    assert "rerank_unavailable" in resp.warnings
    points = metric_points(otel.reader)
    assert sum(p.value for p in points["chatbot.rerank.fallbacks"]) == 1
