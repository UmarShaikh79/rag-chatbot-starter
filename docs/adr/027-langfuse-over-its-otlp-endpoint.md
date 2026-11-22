# ADR 027: Langfuse over its OTLP endpoint

Status: Accepted (milestone 5)

## Decision

Langfuse is a second exporter on the same tracer provider (ADR 026), pointed at Langfuse's
OTLP traces endpoint (`<base_url>/api/public/otel/v1/traces`) with Basic auth from the
project's public and secret keys and `x-langfuse-ingestion-version: 4`. The Langfuse SDK is
not used; `uv sync --extra langfuse` installs the same packages as `otel`.

Spans carry the attributes Langfuse maps: `langfuse.observation.type` (generation,
embedding, retriever, guardrail, evaluator, chain), `gen_ai.request.model`,
`langfuse.observation.usage_details` and `cost_details`, `langfuse.user.id` and
`langfuse.session.id` (the conversation) on every span, `langfuse.environment` and
`langfuse.release`. The Langfuse exporter keeps only spans of the `chatbot` scope and makes
each chat turn or job a root, so HTTP and database spans never reach it. Both backends can
run at once.

`observability.langfuse.capture_text` is on by default (reading conversations is what
Langfuse is for) and `observability.otel.capture_text` is off.

## Alternatives considered

- The Langfuse Python SDK (`@observe`, `propagate_attributes`): a second instrumentation
  path next to OpenTelemetry, and its client in every traced module.
- Sending to Langfuse through a Collector: works (see `deploy/k8s/components/otel-collector`)
  and is better at scale, but a local setup should need only two keys.

## Consequences

- With text capture on, redacted prompts, retrieved chunks and answers leave your
  infrastructure. PII redaction runs first (ADR 015), but documents are not redacted and
  `forget_user` cannot delete what Langfuse stored: delete those traces in Langfuse (by user
  ID) too, or turn `capture_text` or `send_user_id` off.
- Langfuse's OTLP endpoint needs Langfuse >= 3.22 when self-hosted, and HTTP (no gRPC).
- The attribute names come from Langfuse's OpenTelemetry docs (read 2026-11-22). If Langfuse
  renames them, only `chatbot.observability.tracing` and `instrument` change.

Addendum (first run against self-hosted Langfuse 4.83): the export works unchanged. The first
turn of a conversation had its session only on the root span, because the conversation ID was
made inside the orchestrator after the first spans opened. `MeteredOrchestrator` now assigns
it before the root span (a memory store already treats an unseen ID as a new conversation),
so every span of every turn carries the session, as stated above.
