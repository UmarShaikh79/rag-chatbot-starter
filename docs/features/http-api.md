# HTTP API

How the FastAPI app is put together: the app factory, the routes, the middleware that gives
every request a trace ID, how errors become JSON, CORS for browser apps, and how to add a
route of your own. The route-by-route reference (request and response bodies, the streaming
contract) is [HTTP API reference](../api.md); this guide is about the code behind it.

Back to the [docs home](../README.md) · See also: [HTTP API reference](../api.md)

## At a glance

| | |
| --- | --- |
| **What it does** | Serves `/v1/chat` (JSON or SSE), ingestion, jobs, sources, documents, memory and usage over HTTP, with health probes, OpenAPI docs, trace IDs and uniform JSON errors. |
| **Run with** | `chatbot serve` (or `make up` with Docker); `uvicorn chatbot.api.app:create_app --factory` |
| **Main files** | [`api/app.py`](../../src/chatbot/api/app.py) (`create_app`), [`api/middleware.py`](../../src/chatbot/api/middleware.py) (`TraceMiddleware`), [`api/routes/`](../../src/chatbot/api/routes) (`chat`, `ingest`, `memory`, `usage`, `health`), [`api/deps.py`](../../src/chatbot/api/deps.py) (`components`), [`core/errors.py`](../../src/chatbot/core/errors.py) (`describe_error`) |
| **Depends on** | Postgres (`DATABASE_URL`), credentials per `auth.mode` ([authentication](authentication.md)) |
| **Used by** | Your apps, in any language: plain HTTP and server-sent events |
| **Decisions** | ADR [001](../adr/001-template-not-framework.md) (a template, not a framework), ADR [014](../adr/014-streaming-with-output-guards.md) (the streaming contract), ADR [017](../adr/017-jwt-oidc-alongside-api-keys.md) (auth) |

## What it does

| Group | Routes | Who |
| --- | --- | --- |
| Health | `GET /healthz`, `/livez`, `/readyz` | Public, never traced or logged |
| Chat | `POST /v1/chat` | Any credential |
| Memory | `GET /v1/users/{id}/memory`, `DELETE …/memory/facts/{fact_id}`, `DELETE …/memory` | Any credential (end users only for themselves) |
| Ingestion | `POST /v1/ingest`, `GET /v1/jobs/{id}`, `GET /v1/sources`, `DELETE /v1/documents/{id}` | API key or admin token |
| Usage | `GET /v1/usage` | API key or admin token |
| Docs | `/docs`, `/redoc`, `/openapi.json` | Public when `api.docs: true` |

Every response carries an `X-Trace-Id` header, and every error has the same shape. Real
examples from the running API (auth refusals; the full list of statuses is in the
[reference](../api.md)):

```json
{"detail": "missing bearer token", "trace_id": "59f1fe753e4145c6acac8bb03e6940fc"}
{"detail": "this route needs an API key or admin scope", "trace_id": "17ef2f894ab145cd8eec8555bd528aca"}
```

Search your logs (or your tracing backend) for the `trace_id` to find the full story of that
request, including a stack trace the client never sees.

## How it works

**Startup.** `chatbot serve` first runs `_preflight_serve` ([`cli.py`](../../src/chatbot/cli.py)):
missing model keys, `DATABASE_URL`, API-key hashes, a JWT secret or Langfuse keys stop the
process with one readable message instead of a traceback inside uvicorn. Then uvicorn calls
[`create_app(settings)`](../../src/chatbot/api/app.py):

1. Configures logging and builds the `Authenticator`.
2. Creates the FastAPI app with a **lifespan**: on start, telemetry (if on), a Postgres pool,
   and `container.build(settings, pool)`, the [container](../architecture.md#3-the-container-builds-everything)
   that builds every part once; on stop, closes them.
3. Adds `TraceMiddleware`, then CORS (last, so it's the outermost layer).
4. Includes the routers: health (no auth), chat and memory (`Depends(auth)`), ingestion and
   usage (`Depends(auth)` plus `require_admin`).
5. Registers the error handlers.

Routes get the built parts with `c: Components = Depends(components)` and the caller with
`principal: Principal = Depends(current_principal)`.

**Each request**, in [`TraceMiddleware`](../../src/chatbot/api/middleware.py) (a plain ASGI
middleware, so it stays in place for the whole length of a streamed response):

1. Probes (`/healthz`, `/livez`, `/readyz`) get no span, metric or log line.
2. Opens the server span (continuing an incoming W3C `traceparent` when tracing is on); the
   trace ID is the span's, or a safe `X-Request-ID` (8–64 letters, digits, `-`, `_`), or a new
   one. It's stored in a [ContextVar](../glossary.md#contextvar), so every log line in the
   request carries it.
3. Adds `X-Trace-Id` to the response headers.
4. If the app raised something nobody handled and no response has started yet, answers
   `500 {"detail": "internal error", "trace_id": …}` instead of Starlette's plain-text page.
5. Records `http.server.request.duration` and an access log line (method, path, status, ms).

**Errors** (`create_app`'s handlers), as `{"detail", "trace_id"}` except validation errors:

| Raised | Status |
| --- | --- |
| `HTTPException` from routes and auth | its own status (401, 403, 404, 503), headers kept |
| `ForbiddenError` | 403 |
| `NotFoundError` | 404 |
| `IndexMismatchError` | 409 |
| Request validation | 422, FastAPI's own body (`{"detail": [{"loc": ["body", "message"], "msg": …}]}`); the trace ID is only in the `X-Trace-Id` header |
| `ProviderError`, `httpx.HTTPError`, `OpenAIError` | 502, with a short message naming the kind of provider |
| Any other `ChatbotError` | 500, with its message |
| Anything else | 500 `internal error` |

`describe_error` decides 502 versus 500 and writes the safe message; provider SDK messages are
never repeated because they can contain part of an API key.

**Chat** ([`routes/chat.py`](../../src/chatbot/api/routes/chat.py)): resolves the user ID from
the principal, then either `await orchestrator.run(req)` (JSON) or a `StreamingResponse` over
`orchestrator.stream(req)` when `stream: true` or `Accept: text/event-stream`
([answers and streaming](answers-and-streaming.md)).

## Inputs and outputs

**In:** JSON bodies validated by Pydantic models (`ChatRequest`, `IngestRequest`); path and
query parameters; `X-API-Key` / `Authorization`; optional `traceparent` or `X-Request-ID`.

**Out:** JSON (or `text/event-stream` for chat), always with `X-Trace-Id`. The OpenAPI schema at
`/openapi.json` describes every route and model, so you can generate a typed client:

```bash
curl -s localhost:8000/openapi.json -o openapi.json
npx @openapitools/openapi-generator-cli generate -i openapi.json -g typescript-fetch -o client/
```

## Turn it on, change it, turn it off

```bash
uv run chatbot serve --port 8000      # or: make up (Docker: Postgres, API, worker)
```

| Setting | Default | Notes |
| --- | --- | --- |
| `api.docs` | true | `false` hides `/docs`, `/redoc` and `/openapi.json` (e.g. in production). |
| `api.cors.allow_origins` | `[]` (off) | Origins of browser apps allowed to call the API, e.g. `[https://app.example.com]`. |
| `api.cors.allow_credentials` | false | Cookies; not needed for `Authorization` headers. Can't be combined with `["*"]`. |
| `auth.*` | API keys | [Authentication](authentication.md). |
| `observability.log_*` | JSON, INFO, no message text | [Observability](observability.md). |

**Browser apps on another domain** need CORS; servers and mobile apps don't. Never ship an API
key in browser code: use JWTs from your identity provider, or call the API from your backend.
All `api.cors.*` options are in [configuration: `api`](../configuration.md#api).

**Scaling:** the API is stateless (state lives in Postgres), so run more processes or pods
behind a load balancer. Turn proxy buffering off for `/v1/chat` so streams aren't held back
([deployment](deployment.md)).

**Removing routes you don't need:** delete the `include_router` line in `create_app` (for
example usage or ingestion, if you only ingest from the CLI). Tests that call them go with it.

## Extend it

Add a router in your own module and include it in `create_app`:

```python
# src/chatbot/api/routes/feedback.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbot.api.deps import components
from chatbot.auth.principal import Principal, current_principal
from chatbot.core.container import Components

router = APIRouter(prefix="/v1", tags=["feedback"])


class Feedback(BaseModel):
    trace_id: str
    helpful: bool
    comment: str = ""


@router.post("/feedback", status_code=204)
async def feedback(
    body: Feedback,
    c: Components = Depends(components),
    principal: Principal = Depends(current_principal),
) -> None:
    async with c.pool.connection() as conn:  # store it next to the turn's usage record
        await conn.execute(
            "INSERT INTO feedback (trace_id, user_id, helpful, comment) VALUES (%s, %s, %s, %s)",
            (body.trace_id, principal.subject, body.helpful, body.comment),
        )
```

```python
# in create_app (api/app.py)
from chatbot.api.routes import feedback

app.include_router(feedback.router, dependencies=protected)
```

Add the `feedback` table with a migration (`migrations/`, Alembic). Rules worth keeping:

- Choose `protected` or `admin_only` for the router; never leave a `/v1` route unauthenticated.
- Raise `HTTPException` (or `NotFoundError`, `ForbiddenError`) and let the handlers format the
  body, so the trace ID is always there.
- Use parts from `Components`, not new clients: they're configured, traced and closed on
  shutdown.

## Test it

```bash
uv run pytest tests/unit/test_api.py tests/unit/test_api_errors.py tests/unit/test_api_cors.py
```

| Test file | Covers |
| --- | --- |
| [`test_api.py`](../../tests/unit/test_api.py) | health and the trace header; ingest then chat; the SSE contract; inline documents and sources; unknown source 404; forgetting a user; API-key auth; a wrong owner gets 403 |
| [`test_api_errors.py`](../../tests/unit/test_api_errors.py) | provider failures are 502; our own provider errors keep their message; SDK messages are never shown; template errors are 500 with their message; unexpected errors are JSON 500 without internals |
| [`test_api_cors.py`](../../tests/unit/test_api_cors.py) | CORS off by default; a listed origin can call the API; other origins are refused; `["*"]` with credentials is rejected; `/docs` can be turned off |
| [`test_auth.py`](../../tests/unit/test_auth.py) | auth refusals carry the trace ID |

The tests build the app with `create_app(settings, components=…)` and in-memory parts, so they
need no database.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `chatbot serve` stops with a one-line message | A preflight check: a missing key, `DATABASE_URL` or API-key hash | Do what the message says. |
| The browser console shows a CORS error | The app's origin isn't allowed | Add it to `api.cors.allow_origins`. |
| 422 with a list of fields (no `trace_id` in the body) | The body doesn't match the model | Compare with `/docs`; `message` must be non-empty. The trace ID is in the `X-Trace-Id` header. |
| 502 on chat | The model, embedding or rerank provider failed | The `detail` says which; the log line with the trace ID has the cause. |
| 409 on chat | The index was built with another embedding model | [Re-indexing](reindexing.md). |
| `/docs` returns 404 | `api.docs: false` | Expected; turn it on where you need it. |
| Streams arrive in one piece through a proxy | Proxy buffering | Turn it off for `/v1/chat` ([deployment](deployment.md)). |

## Related

- Every route's bodies and statuses: [HTTP API reference](../api.md).
- Who may call what: [authentication](authentication.md).
- What happens inside a chat request: [orchestration](orchestration.md),
  [answers and streaming](answers-and-streaming.md).
- Trace IDs, logs and metrics: [observability](observability.md).
- Terms: [SSE](../glossary.md#sse), [trace ID](../glossary.md#trace-id),
  [container](../glossary.md#container).
