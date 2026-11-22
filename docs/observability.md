# Observability: logs, traces, metrics and cost

Four layers, each optional except the first:

| Layer | On by default | Turn on with | Where it goes |
| --- | --- | --- | --- |
| JSON logs with a trace ID | yes | `observability.log_*` | stdout |
| Token and cost accounting | yes (with Postgres) | `observability.usage.persist` | `usage_records`, `GET /v1/usage`, `chatbot usage` |
| OpenTelemetry traces and metrics | no | `observability.otel.enabled` + `uv sync --extra otel` | any OTLP backend |
| Langfuse | no | `observability.langfuse.enabled` + `uv sync --extra langfuse` | Langfuse Cloud or self-hosted |

## Trace IDs

Every request and job has a trace ID. It is in the `X-Trace-Id` response header, in every chat
response and SSE terminal event (`trace_id`), in every log line, and on every usage record.
With tracing on it is the OpenTelemetry trace ID, and an incoming W3C `traceparent` header is
continued, so a trace started in your app carries on through the chatbot. With tracing off, a
safe `X-Request-Id` is reused, or a new ID is made.

## Try it locally

```bash
# Jaeger and an OpenTelemetry Collector in Docker, the app built with the otel extra
echo "CHATBOT__OBSERVABILITY__OTEL__ENABLED=true" >> .env
make up-otel
make ingest && make ask KEY=cb_... Q="How long is the refund window?"
open http://localhost:16686          # traces: service rag-chatbot
curl -s localhost:8889/metrics | grep chatbot_
```

Without Docker: `uv sync --extra otel`, point `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector
(or set `observability.otel.console: true` to print spans), and run as usual.

## What a chat turn looks like

```
POST /v1/chat                       server span (HTTP method, route, status)
└── chat                            the turn: kind, stream, orchestrator, usage, cost
    ├── input guards                action, reasons
    ├── load memory                 turns, facts, summary size
    ├── chat gpt-…  (rewrite)       only with retrieval.query_rewrite
    ├── retrieve                    scorer, top_k, hits
    │   └── embeddings text-embedding-3-small
    ├── context guards              chunks dropped
    ├── answer                      sources, citations
    │   └── chat gpt-…              model, purpose, tokens, cost, time to first token
    ├── groundedness                only with guardrails.output.groundedness_check
    └── output guards
```

Model spans follow the OpenTelemetry GenAI conventions (`gen_ai.operation.name`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) plus
`chatbot.purpose` (answer, clarify, hyde, rewrite_query, summarize, extract_facts,
guard_injection, judge_groundedness, documents, query) and `chatbot.model_role`.

Background jobs are traces of their own (`job:sync_source`, `job:ingest_documents`,
`job:memory_refresh`), as is `chatbot ingest --inline` (`ingest:<source>`).

### Text on spans

Off by default for OpenTelemetry (`otel.capture_text: false`): spans carry sizes, scores,
models, tokens and cost. Turn it on to see prompts, retrieved chunk IDs and scores, and answers.
The text is always what the guards produced, so PII in user messages is already redacted.
Retrieved documents are not redacted.

User and session are on every span whatever `capture_text` says: `langfuse.user.id` (the
request's `user_id`, or the token's `sub`) and `langfuse.session.id` (the conversation), so any
OpenTelemetry backend (Jaeger, Tempo, Honeycomb) stores them too. `langfuse.send_user_id`
only affects what Langfuse gets, and `forget_user` cannot delete spans in those backends:
use your own user IDs rather than emails, and set the backend's retention to match your
privacy policy.

FastAPI 0.165+ can trace and meter requests itself. The app turns that off
(`api/app.py`, `_fastapi_telemetry`) because the template's middleware already makes the server
span and the HTTP metric: left on, every request became two traces, was counted twice, and
every health check became a trace. Probes (`/healthz`, `/livez`, `/readyz`) are never traced.

Querying Jaeger from a script: Jaeger 2.x serves its API under `/api/v3/...`
(`/api/v3/services`, `/api/v3/traces/<trace_id>`).

## Metrics

| Metric | Type | Attributes |
| --- | --- | --- |
| `chatbot.chat.turns` | counter | `chatbot.kind` (answer, clarify, refuse, error, cancelled), `chatbot.stream`, `chatbot.orchestrator` |
| `chatbot.chat.duration` | histogram (s) | same |
| `chatbot.retrieval.requests` | counter | `chatbot.retrieval.weak`, `chatbot.retrieval.gate` |
| `chatbot.guard.blocks` | counter | `chatbot.guard.stage` (input, context, output) |
| `gen_ai.client.token.usage` | histogram | `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.token.type`, `chatbot.purpose` |
| `gen_ai.client.operation.duration` | histogram (s) | same, plus `error.type` |
| `chatbot.cost` | counter (USD) | `gen_ai.request.model`, `chatbot.purpose` (only priced models) |
| `chatbot.jobs` | counter | `chatbot.job.kind`, `chatbot.job.status` |
| `chatbot.jobs.duration` | histogram (s) | `chatbot.job.kind` |
| `http.server.request.duration` | histogram (s) | `http.request.method`, `http.route`, `http.response.status_code` |

Useful alerts: the share of `chatbot.chat.turns` with `kind=clarify` rising (retrieval got
worse, or users ask about something you have not ingested), `chatbot.guard.blocks` spiking,
`chatbot.jobs` with `status=failed`, and `chatbot.cost` per hour.

## Sending traces somewhere

The exporter reads the standard variables:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.example.com     # /v1/traces and /v1/metrics are added
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20<token>
OTEL_SERVICE_NAME=support-bot                             # default: app.name
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=staging
```

Only OTLP over HTTP is built in (no gRPC dependency). In production, send to an
OpenTelemetry Collector next to the app and let it fan out, sample and retry:
`deploy/k8s/components/otel-collector` is a starting configuration.

`observability.otel.sample_ratio` keeps that share of new traces; a sampled parent (an
incoming `traceparent`) always wins.

## Langfuse

```bash
uv sync --extra langfuse
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com    # or https://us.cloud.langfuse.com, or yours
CHATBOT__OBSERVABILITY__LANGFUSE__ENABLED=true uv run chatbot serve
```

Each chat turn becomes a Langfuse trace with the user (`user_id`) and session (the
conversation), generations with model, tokens and cost, and the retrieval, guard and
groundedness steps as typed observations. Only the chatbot's own spans are sent; HTTP spans
(and any library spans you add) are not. It works alongside `otel` (both can be on). Self-hosted Langfuse
needs version 3.22 or later (tested with 4.83). See ADR 027.

Running Langfuse's own Compose file next to this one: keep it on its own network and point
`LANGFUSE_BASE_URL` at the published port (`http://host.docker.internal:3000` from a
container). Both stacks have a service named `postgres`; on one network Langfuse resolves the
chatbot's database and fails to start. Langfuse 4 reads observations through
`GET /api/public/v2/observations` (the older `/api/public/traces` is gone).

Privacy: Langfuse gets text by default (`langfuse.capture_text: true`) because reading
conversations is what it is for. The user messages are redacted first; retrieved documents
are not. `forget_user` cannot delete traces in Langfuse: delete them there by user ID as well,
or set `capture_text: false` or `send_user_id: false`.

## Cost and token accounting

Fill in `price_per_mtok` for your models in `config/models.yaml` (USD per million tokens; they
ship `null` because prices change). Then:

- every chat response has `usage.cost_usd`,
- every chat turn, job and `ingest --inline` writes one `usage_records` row with totals and a
  per-call breakdown (model, role, purpose, tokens, cost, duration),
- reports sum them:

```bash
chatbot usage                      # last 30 days by day
chatbot usage --by user --days 7   # who spent what
chatbot usage --by model           # which model costs most
curl -s "localhost:8000/v1/usage?group_by=operation&since=2026-11-01T00:00:00Z" -H "X-API-Key: $KEY"
```

Calls to a model with no price are counted in `unpriced_calls`, so a report never shows a cost
that silently leaves them out. `forget_user` keeps the totals but removes the user and
conversation IDs from that user's rows. Eval runs write no records. See ADR 028.

## Adding your own spans and metrics

```python
from chatbot.observability.tracing import count, observe, span

with span("crm lookup", type="tool", attrs={"crm.account": account_id}) as s:
    record = await crm.get(account_id)
    s.io(output={"plan": record.plan})  # only recorded when a backend captures text
count("crm.lookups", 1, {"crm.hit": record is not None})
```

They do nothing until a backend is on. Any model or embedder the container builds is traced
and priced already, including providers you add. Library instrumentation (httpx, psycopg,
FastAPI) can be added on top: the tracer provider is installed as OpenTelemetry's global one.
