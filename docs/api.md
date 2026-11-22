# HTTP API

All `/v1` routes need credentials unless `auth.mode: none`: an API key (`X-API-Key` or
`Authorization: Bearer <key>`) or, in the JWT modes, a token (`Authorization: Bearer <jwt>`).
API keys can call every route for any user. A JWT end user always acts as the token's subject
and can only use chat and their own memory routes; ingestion, jobs, sources and documents need
an API key or the admin scope. Interactive docs are served at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Database check (public); same as `/readyz` |
| GET | `/livez` | Liveness: the process answers, no dependency checked (public) |
| GET | `/readyz` | Readiness: the database answers (public) |
| POST | `/v1/chat` | Ask a question (JSON, or SSE with `stream: true` / `Accept: text/event-stream`) |
| POST | `/v1/ingest` | Queue a sync of a configured source, or ingest inline documents |
| GET | `/v1/jobs/{id}` | Job status and result report |
| GET | `/v1/sources` | Configured sources, inline sources, document and chunk counts, index info (embedding model, active retrieval scorer, calibrated thresholds) |
| DELETE | `/v1/documents/{document_id}` | Remove one document and its chunks |
| GET | `/v1/users/{user_id}/memory` | Long-term facts remembered about the user, most recent first |
| DELETE | `/v1/users/{user_id}/memory/facts/{fact_id}` | Forget one fact (204) |
| DELETE | `/v1/users/{user_id}/memory` | Delete everything stored for a user; returns counts per table |
| GET | `/v1/usage` | Tokens and cost by `group_by` = `day`, `user`, `model` or `operation`; `since`, `until`, `user_id` filters (admin) |

Errors return `{"detail": "...", "trace_id": "..."}`: 401 bad key or token, 403 conversation
owned by another user (or a JWT user reaching for another user or an admin route), 404 unknown
source/job/document/fact, 409 index built with a different embedding model, 422 invalid body,
500 something failed inside the service (`detail` is the template's own message, or
`internal error`), 502 a model, embedding or rerank provider failed after its retries
(`detail` says which kind; SDK messages are not repeated because they can contain part of an
API key), 503 a token could not be checked because the identity provider's signing keys could
not be fetched (with `Retry-After`; not a sign-out). Every one has the `X-Trace-Id` header; the
log line with the same trace ID has the full error. A streamed request reports the same
`detail` in its `error` event.

## POST /v1/chat

```json
{
  "message": "How long is the refund window?",
  "user_id": "customer-42",          // optional; your app's ID for the end user (JWT: the token's sub)
  "conversation_id": null,           // omit to start a conversation; reuse the returned one
  "filters": {"source": "samples"},  // optional; see "Filters" below
  "stream": false
}
```

Filters (`filters`, the same in every vector store): `document_id` takes one ID or a list.
Any other key matches chunk metadata, and all keys must match. A single value matches the
key's value or any element of a list value (`{"tags": "faq"}` matches `"faq"` and
`["faq", "billing"]`); a list requires every element (`{"tags": ["faq", "billing"]}`).
Values are compared with their JSON type (`7` is not `"7"`). A filtered question is held to
`retrieval.min_hits_filtered` instead of the corpus-wide `min_hits` (see configuration.md).

Response (`ChatResponse`):

| Field | Meaning |
| --- | --- |
| `answer` | Text. For `kind: answer` it contains `[n]` markers. |
| `citations` | `[{marker, chunk_id, document_id, title, uri, score, snippet}]` for each marker used |
| `kind` | `answer`, `clarify` (retrieval was weak or the model abstained; `answer` is a question back to the user) or `refuse` (blocked by an input or output guard) |
| `conversation_id` | Pass it back to continue the conversation |
| `trace_id` | Same as the `X-Trace-Id` header; search logs by it |
| `usage` | `prompt_tokens`, `completion_tokens`, `embedding_tokens`, `cost_usd` (null unless prices are set) |
| `warnings` | e.g. `pii:EMAIL` when the question was redacted before use, `injection:override_instructions` or `prompt_leak` with a refusal, `rerank_unavailable` when the reranker failed and retrieval order was used |

Filters: `document_id` (string or list) matches documents; every other key must equal the
chunk metadata value (`source`, `title`, `heading`, `page`, `uri`, or any metadata you attached
to inline documents).

## Streaming contract (ADR 014)

With `stream: true` the response is `text/event-stream`. Events:

| Event | Data | Notes |
| --- | --- | --- |
| `delta` | `{"text": "..."}` | Draft text, already PII-redacted. Zero or more. |
| `final` | `ChatResponse` fields | The drafts concatenate to `answer`. Ends the stream. |
| `replace` | `ChatResponse` fields | A guard or citation check changed the text. **Replace the draft** with `answer`. Ends the stream. |
| `refusal` | `ChatResponse` fields, `kind: refuse` | The request or the finished answer was blocked. **Discard any draft.** Ends the stream. |
| `error` | `{"message", "trace_id"}` | Something failed. Ends the stream. |

Exactly one of `final`, `replace`, `refusal`, `error` ends every stream. Treat deltas as a
draft; the terminal event is the answer. Typical causes of `replace`: the model cited a source
that does not exist, the model answered without citations (the answer becomes a clarifying
question), or the model abstained.

With `answer.stream_mode: buffered`, streams contain only the terminal event. Use it when
unchecked text must never be shown, even briefly.

Minimal browser client:

```js
const res = await fetch("/v1/chat", {
  method: "POST",
  headers: {"Content-Type": "application/json", "X-API-Key": key},
  body: JSON.stringify({message, conversation_id, stream: true}),
});
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
let buf = "", draft = "";
for (;;) {
  const {value, done} = await reader.read();
  if (done) break;
  buf += value;
  let i;
  while ((i = buf.indexOf("\n\n")) >= 0) {
    const block = buf.slice(0, i); buf = buf.slice(i + 2);
    const event = block.match(/^event: (.*)$/m)[1];
    const data = JSON.parse(block.match(/^data: (.*)$/m)[1]);
    if (event === "delta") { draft += data.text; render(draft); }
    else if (event === "final" || event === "replace" || event === "refusal") render(data.answer, data);
    else showError(data);
  }
}
```

## POST /v1/ingest

Sync a configured source (only changed documents are re-embedded; documents removed from the
source are deleted from the index):

```json
{"source": "samples", "force": false}
```

Or push documents from your own systems (up to 500 per call). IDs are prefixed with the source
label, so `{"source": "crm", "documents": [{"id": "42"}]}` becomes `crm:42`:

```json
{
  "source": "crm",
  "user_id": null,
  "documents": [
    {"id": "42", "title": "Order 42", "text": "Markdown or plain text", "media_type": "text/markdown",
     "uri": "https://crm.example/orders/42", "metadata": {"region": "eu"}}
  ]
}
```

Both return `202 {"job_id", "status_url"}`. The worker picks the job up; `GET /v1/jobs/{id}`
returns `status` (`queued`, `running`, `succeeded`, `failed`), `attempts`, `last_error` and a
`result` report (`indexed`, `unchanged`, `removed`, `failed`, `chunks`, `usage`). Error texts
in `last_error` and `result.failed` are `"Type: message"` with credentials replaced by
`[redacted]` (an OpenAI 401 message, for one, repeats part of the key); the full error is in the
worker's log line.

## Memory

With a `user_id` (or a JWT), each turn feeds two kinds of memory (ADR 016): a rolling summary of
the conversation's older turns and long-term facts about the user ("The user is on the Team
plan"), both redacted before storage. They are refreshed by the worker after each turn.

`GET /v1/users/{user_id}/memory`:

```json
{"user_id": "customer-42",
 "facts": [{"id": 7, "fact": "The user is on the Team plan.", "source_conversation_id": "9f...",
            "created_at": "...", "updated_at": "..."}]}
```

Show these to the user if you like; `DELETE /v1/users/{user_id}/memory/facts/7` removes one.
`DELETE /v1/users/{user_id}/memory` removes everything and returns counts per table:

```json
{"user_id": "customer-42",
 "deleted": {"user_facts": 3, "conversation_summaries": 1, "turns": 24, "conversations": 2,
             "jobs": 0, "documents": 0}}
```

With Qdrant as the store, the user's vectors are deleted from it first and counted as
`vector_points`. Usage records are kept for accounting but unlinked from the user
(`usage_records_anonymised`).

## Tracing headers

Every response carries `X-Trace-Id`. Send a W3C `traceparent` header and, with tracing on, the
chatbot's spans join your trace and `trace_id` is that trace's ID. Without tracing, an
`X-Request-Id` of 8-64 letters, digits, `-` or `_` is used as the trace ID.

## GET /v1/usage

Admin only (API key or the admin scope). Sums `usage_records` (ADR 028):

```bash
curl -s "localhost:8000/v1/usage?group_by=model&since=2026-11-01T00:00:00Z" -H "X-API-Key: $KEY"
```

```json
{"group_by": "model", "since": "2026-11-01T00:00:00Z", "until": null,
 "rows": [{"key": "gpt-…", "operations": 412, "prompt_tokens": 210554, "completion_tokens": 18210,
           "embedding_tokens": 0, "cost_usd": 0.41, "unpriced_calls": 0}],
 "total": {"key": null, "operations": 412, "...": "..."}}
```

`operations` counts turns and jobs, or calls when grouped by model. `cost_usd` sums priced calls
only; `unpriced_calls` says how many had no price in `config/models.yaml`. Returns 404 when
records are off (`observability.usage.persist: false`, or no Postgres).
