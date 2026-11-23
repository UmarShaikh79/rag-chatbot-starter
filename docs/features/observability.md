# Observability

How you see what the bot did: JSON logs with a trace ID on every line, and optionally
OpenTelemetry traces and metrics (Jaeger, Tempo, Honeycomb, Datadog, …) or Langfuse. This guide
follows the code; the full list of spans, metrics and backend recipes is in the
[observability reference](../observability.md).

Back to the [docs home](../README.md) · See also: [observability reference](../observability.md)

## At a glance

| | |
| --- | --- |
| **What it does** | Gives every request, job and CLI run a trace ID; logs as JSON lines; when a backend is on, records a span per step (guards, retrieval, rerank, each model call) and metrics (turns, weak retrieval, guard blocks, tokens, cost, jobs, HTTP). |
| **Turned on by** | Logs: always. Traces and metrics: `observability.otel.enabled` (`uv sync --extra otel`) and/or `observability.langfuse.enabled` (`uv sync --extra langfuse`) |
| **Main files** | [`observability/logging.py`](../../src/chatbot/observability/logging.py) (`configure_logging`, the trace-ID ContextVar), [`observability/tracing.py`](../../src/chatbot/observability/tracing.py) (`span`, `count`, `observe`, `configure_telemetry`, `build_otel`), [`observability/instrument.py`](../../src/chatbot/observability/instrument.py) (`MeteredLLM`, `MeteredEmbedder`, `MeteredOrchestrator`), [`api/middleware.py`](../../src/chatbot/api/middleware.py) (`TraceMiddleware`), `scrub_secrets` in [`core/errors.py`](../../src/chatbot/core/errors.py) |
| **Depends on** | Nothing for logs; an OTLP endpoint (a Collector) or Langfuse for traces |
| **Used by** | Every part: the orchestrator steps, retrieval, rerank, guards, the worker, the CLI |
| **Decisions** | ADR [026](../adr/026-tracing-and-metrics-through-a-small-facade-on-opentelemetry.md) (a small facade on OpenTelemetry), ADR [027](../adr/027-langfuse-over-its-otlp-endpoint.md) (Langfuse over its OTLP endpoint), ADR [028](../adr/028-usage-records-per-operation.md) (usage records) |

## What it does

| Layer | Default | Where it goes | Answers |
| --- | --- | --- | --- |
| Logs | on, JSON, no message text | stdout | What happened, with which trace ID? |
| Traces | off | Any OTLP/HTTP backend, Langfuse | Where did this turn spend its time, which passages did it see, which step refused it? |
| Metrics | off (with traces) | OTLP, e.g. Prometheus via a Collector | How many turns clarify, how often guards block, tokens and cost per model |
| Usage records | on (with Postgres) | `usage_records` | What did each turn and job cost? ([usage and cost](usage-and-cost.md)) |

The **trace ID** ties them together: it's in the `X-Trace-Id` header, in every chat response
and SSE terminal event (`trace_id`), on every log line, on every usage record, and, with
tracing on, it **is** the OpenTelemetry trace ID. A user reports a bad answer with its trace ID;
you find the log lines, the trace with every step, and its cost.

**Privacy by default:** logs never contain user or model text unless
`observability.log_message_text: true`; OpenTelemetry spans carry text only with
`otel.capture_text: true`, and then only the redacted text; credentials in error messages are
replaced by `[redacted]` before export. Langfuse gets redacted text by default, because reading
conversations is what it's for.

## How it works

**Logs.** `configure_logging(level, format)` installs `JsonFormatter` (or `TextFormatter`) on the
root logger. Each line gets the current trace ID from a [ContextVar](../glossary.md#contextvar)
set by `TraceMiddleware` (requests), the worker (jobs) or the CLI. Extra fields passed with
`log.info("…", extra={...})` become JSON keys.

**The facade.** Code everywhere calls three functions from
[`observability/tracing.py`](../../src/chatbot/observability/tracing.py):

```python
with span("retrieve", type="retriever", attrs={"chatbot.retrieval.top_k": 8}) as s:
    hits = ...
    s.set({"chatbot.retrieval.hits": len(hits)})
    s.io(input=query, output=[h.chunk.id for h in hits])  # text: only if capture_text
count("chatbot.retrieval.requests", 1, {"chatbot.retrieval.weak": False})
observe("chatbot.chat.duration", 1.42, {"chatbot.kind": "answer"})
```

Until a backend is configured they do nothing, and OpenTelemetry isn't even imported, so the
base install stays light and the code has no `if tracing_enabled:` branches.

**Turning a backend on.** At startup (API lifespan, worker, CLI) `configure_telemetry(settings,
role)` either keeps the no-op `Telemetry`, or calls `build_otel`, which creates one
OpenTelemetry tracer provider (and meter provider) with an exporter per backend: OTLP/HTTP for
`otel` (reading the standard `OTEL_EXPORTER_OTLP_*` variables), Langfuse's OTLP endpoint for
`langfuse` (a filtering exporter that keeps only the chatbot's own spans and drops text if
`capture_text` is off). The provider is also installed as OpenTelemetry's global one, so library
instrumentation you add lands in the same traces.

**Who makes which spans:**

| Span | Made by |
| --- | --- |
| `POST /v1/chat` (server) | `TraceMiddleware`, continuing an incoming `traceparent` |
| `chat` (the turn, root for Langfuse) | `MeteredOrchestrator` |
| `input guards`, `load memory`, `retrieve`, `rerank`, `context guards`, `answer`, `clarify`, `groundedness`, `output guards` | the orchestrator steps and the retrieval pipeline |
| `chat <model>`, `embeddings <model>` | `MeteredLLM`, `MeteredEmbedder`, which the container wraps around every model, so providers you add are traced too |
| `rerank <model>` | `CohereReranker` (attempts, billed units, cost) |
| `job:<kind>`, `ingest:<source>` | the worker, `chatbot ingest --inline` |

**Failures:** `span.fail(exc)` marks the span as an error and records the exception, with
credentials scrubbed (`scrub_secrets`: provider keys, `cb_` keys, JWTs, bearer tokens, URL
passwords).

## Inputs and outputs

**In:** settings, `OTEL_*` / `LANGFUSE_*` environment variables, an incoming `traceparent` or
`X-Request-ID`.

**Out:** a log line per event, for example (text format, from a real run):

```text
INFO    chatbot.access: request trace_id=ad36247f8de0 method=POST path=/v1/chat status=422 ms=2.5
```

and, with a backend on, spans and metrics in it. The span tree of a chat turn and every metric
with its attributes are listed in the
[observability reference](../observability.md#what-a-chat-turn-looks-like).

## Turn it on, change it, turn it off

**Locally, with Jaeger:**

```bash
echo "CHATBOT__OBSERVABILITY__OTEL__ENABLED=true" >> .env
make up-otel                       # Collector + Jaeger, the app built with the otel extra
open http://localhost:16686        # traces
curl -s localhost:8889/metrics | grep chatbot_
```

**To your backend:** `uv sync --extra otel`, `observability.otel.enabled: true`, and the standard
variables:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.example.com
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20<token>
```

In production, send to an OpenTelemetry Collector next to the app
([`deploy/k8s/components/otel-collector`](../../deploy/k8s/components/otel-collector)).

**Langfuse:** `uv sync --extra langfuse`, `observability.langfuse.enabled: true`, and
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`. Both backends can be on
together.

| Setting | Default | Notes |
| --- | --- | --- |
| `observability.log_level` / `log_format` | `INFO` / `json` | `text` for reading in a terminal (the `dev` profile). |
| `observability.log_message_text` | false | Log questions and answers (redacted). |
| `observability.otel.enabled` / `traces` / `metrics` | false / true / true | |
| `observability.otel.sample_ratio` | 1.0 | Share of new traces kept. |
| `observability.otel.capture_text` | false | Redacted prompts, answers, chunk IDs on spans. |
| `observability.otel.console` | false | Also print spans to stdout. |
| `observability.langfuse.enabled` / `capture_text` / `send_user_id` | false / true / true | |

All keys: [configuration: `observability`](../configuration.md#observability).

**Turning it off:** both `enabled: false` (the default) and the extras uninstalled. Logs stay.

## Extend it

- **Your own spans and metrics** in code you add (a CRM lookup, a tool call):

  ```python
  from chatbot.observability.tracing import count, span

  with span("crm lookup", type="tool", attrs={"crm.account": account_id}) as s:
      record = await crm.get(account_id)
      s.io(output={"plan": record.plan})
  count("crm.lookups", 1, {"crm.hit": record is not None})
  ```

  They nest under the current turn automatically and cost nothing while tracing is off.
- **Library instrumentation** (httpx, psycopg): install the OpenTelemetry instrumentation
  package and call its `instrument()` after `configure_telemetry`; its spans join the turns.
- **Another backend:** anything that accepts OTLP needs no code, only `OTEL_EXPORTER_OTLP_*`.
  For one that doesn't, add an exporter to the tracer provider in `build_otel`.
- **Log fields:** pass `extra={...}` to any `log` call; `JsonFormatter` adds them as keys.

## Test it

```bash
uv run pytest tests/unit/test_observability.py
```

[`test_observability.py`](../../tests/unit/test_observability.py), against an in-memory exporter:
off by default is a no-op; a chat turn is one trace with its steps and a generation; captured
text is the redacted text; a streamed turn has one root and records its kind; a failed turn
marks the span; the server span continues `traceparent` and skips probes; a worker job is a
root span with a usage record; Langfuse export keeps only chatbot spans; a backend without
`capture_text` gets no text; FastAPI's own request telemetry doesn't double-count; rerank is
its own span and a fallback is marked; failed spans carry no credentials.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `observability.otel.enabled` but no traces | Extra not installed, or nothing at the endpoint | `uv sync --extra otel`; check `OTEL_EXPORTER_OTLP_ENDPOINT`; try `otel.console: true`. |
| Startup fails with Langfuse on | Missing `LANGFUSE_*` keys | Set both keys and the base URL. |
| Langfuse traces show no text | `langfuse.capture_text: false` | Expected; turn it on if you accept sending redacted text. |
| Two traces per request | Old instrumentation also tracing FastAPI | The app turns FastAPI's own request telemetry off; remove other request instrumentation. |
| A trace ID from a client isn't reused | Tracing is on: the trace ID comes from `traceparent`; or the `X-Request-ID` isn't 8–64 safe characters | Send `traceparent`, or a valid `X-Request-ID`. |
| No user on spans | The request had no `user_id` | Pass `user_id`, or use JWTs. |

## Related

- Every span, metric and backend recipe: [observability reference](../observability.md).
- What each turn costs: [usage and cost](usage-and-cost.md).
- Trace IDs in responses and errors: [HTTP API](http-api.md).
- Running a Collector in Kubernetes: [deployment](deployment.md).
- Terms: [trace ID](../glossary.md#trace-id), [ContextVar](../glossary.md#contextvar).
