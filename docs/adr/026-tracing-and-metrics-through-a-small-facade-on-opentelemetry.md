# ADR 026: Tracing and metrics through a small facade on OpenTelemetry

Status: Accepted (milestone 5)

## Decision

Code calls three functions from `chatbot.observability.tracing`: `span(...)`, `count(...)`
and `observe(...)`. They are no-ops until `configure_telemetry` turns a backend on, and
OpenTelemetry is not imported until then, so the base install has no tracing dependency
(`uv sync --extra otel` adds the SDK and the OTLP/HTTP exporter).

With `observability.otel.enabled`, one tracer provider exports over OTLP/HTTP to wherever the
standard `OTEL_EXPORTER_OTLP_*` variables point (a Collector, Tempo, Honeycomb, Jaeger, ...),
and a meter provider exports the metrics listed in `docs/observability.md`.

What gets a span:

- each HTTP request (a server span from `TraceMiddleware` that continues an incoming W3C
  `traceparent`; health probes are skipped),
- each chat turn (`chat`, a root span from `MeteredOrchestrator`, whichever orchestrator is
  configured) with a child per step: input guards, memory load, retrieve, context guards,
  answer or clarify, groundedness, output guards,
- each model call (`chat <model>`, `embeddings <model>`) from wrappers the container puts
  around every LLM and embedder, with `gen_ai.*` attributes, tokens and cost,
- each background job (`job:<kind>`) and `chatbot ingest --inline`.

The chatbot's `trace_id` (responses, logs, `X-Trace-Id`) becomes the OpenTelemetry trace ID
when tracing is on, so an ID from a support ticket opens the trace directly.

Text (prompts, answers, chunks) goes on spans only through `SpanHandle.io` and only for a
backend with `capture_text` on; other backends get the span with those attributes removed.
The text recorded is always what the guards produced (PII redacted), never the raw message.

## Alternatives considered

- Depending on `opentelemetry-api` directly everywhere: the standard for libraries, but it
  puts a dependency and OTel types in every module of a template meant to be cut apart.
- Auto-instrumentation (`opentelemetry-instrument`, FastAPI/httpx/psycopg instrumentors):
  more spans for free, but noisy, and it cannot know about retrieval steps or costs. It still
  works on top: the tracer provider is installed globally.
- A vendor SDK (Datadog, New Relic): one vendor; OTLP reaches all of them.

## Consequences

- Providers and stores have no telemetry code; a new provider is traced by the wrapper.
- Spans of a streamed turn stay open until the terminal event; a client that disconnects
  ends the turn as `cancelled`.
- The facade is small on purpose. Code that needs more (links, events, baggage) can use the
  OpenTelemetry API directly once the extra is installed.
