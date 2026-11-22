# HTTP API

All `/v1` routes need an API key (`X-API-Key` or `Authorization: Bearer`) unless
`auth.mode: none`. Interactive docs are served at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Liveness and database check (public) |
| POST | `/v1/chat` | Ask a question (JSON, or SSE with `stream: true` / `Accept: text/event-stream`) |
| POST | `/v1/ingest` | Queue a sync of a configured source, or ingest inline documents |
| GET | `/v1/jobs/{id}` | Job status and result report |
| GET | `/v1/sources` | Configured sources, inline sources, document and chunk counts, index info |
| DELETE | `/v1/documents/{document_id}` | Remove one document and its chunks |
| DELETE | `/v1/users/{user_id}/memory` | Delete everything stored for a user; returns counts per table |

Errors return `{"detail": "...", "trace_id": "..."}`: 401 bad key, 403 conversation owned by
another user, 404 unknown source/job/document, 409 index built with a different embedding
model, 422 invalid body.

## POST /v1/chat

```json
{
  "message": "How long is the refund window?",
  "user_id": "customer-42",          // optional; your app's ID for the end user
  "conversation_id": null,           // omit to start a conversation; reuse the returned one
  "filters": {"source": "samples"},  // optional; metadata containment, plus document_id
  "stream": false
}
```

Response (`ChatResponse`):

| Field | Meaning |
| --- | --- |
| `answer` | Text. For `kind: answer` it contains `[n]` markers. |
| `citations` | `[{marker, chunk_id, document_id, title, uri, score, snippet}]` for each marker used |
| `kind` | `answer`, `clarify` (retrieval was weak or the model abstained; `answer` is a question back to the user) or `refuse` (blocked by a guard) |
| `conversation_id` | Pass it back to continue the conversation |
| `trace_id` | Same as the `X-Trace-Id` header; search logs by it |
| `usage` | `prompt_tokens`, `completion_tokens`, `embedding_tokens`, `cost_usd` (null unless prices are set) |
| `warnings` | e.g. `pii:EMAIL` when the question was redacted before use |

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
| `refusal` | `ChatResponse` fields, `kind: refuse` | The request was blocked. Ends the stream. |
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
`result` report (`indexed`, `unchanged`, `removed`, `failed`, `chunks`, `usage`).
