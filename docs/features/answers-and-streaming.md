# Answers and streaming

How an answer is written, cited, checked and delivered: the model answers from numbered
sources, `[n]` markers become citations, guards check the finished text, and with streaming
the words reach the user as they're written while the checked answer still has the last word.

Back to the [docs home](../README.md) · See also: [HTTP API: streaming contract](../api.md#streaming-contract-adr-014)

## At a glance

| | |
| --- | --- |
| **What it does** | Turns strong retrieval into a cited answer (`kind: answer`), or a clarifying question when the model abstains or cites nothing; delivers it as one JSON response or as server-sent events. |
| **Turned on by** | Always on; `answer.*` and `guardrails.stream_holdback_chars` in [`config/app.yaml`](../../config/app.yaml); per request, `"stream": true` or `Accept: text/event-stream` |
| **Main files** | `SimpleOrchestrator.answer`, `_generate`, `_stream_drafts`, `check_output` and `terminal` in [`orchestration/simple.py`](../../src/chatbot/orchestration/simple.py); [`orchestration/citations.py`](../../src/chatbot/orchestration/citations.py) (`format_sources`, `align`); `StreamRedactor` in [`guardrails/pii.py`](../../src/chatbot/guardrails/pii.py); [`api/routes/chat.py`](../../src/chatbot/api/routes/chat.py) (`chat`, `sse`); `ChatResponse`, `Citation`, `ChatEvent` in [`core/types.py`](../../src/chatbot/core/types.py) |
| **Depends on** | [Weak retrieval](weak-retrieval-and-calibration.md) (only strong retrieval reaches the answer), the [answer prompt](prompts.md), the `answer` [model](models-and-providers.md) |
| **Used by** | `POST /v1/chat`, `chatbot ask` |
| **Decisions** | ADR [014](../adr/014-streaming-with-output-guards.md) (streaming with output guards), ADR [006](../adr/006-weak-retrieval-asks-never-invents.md) (ask, never invent) |

## What it does

**Every answer is cited.** The model sees the passages numbered `[1]`, `[2]`, … and must cite
the ones it uses. Each `[n]` in the answer becomes a citation with the document, title, link,
score and a snippet, so a client can show where each claim came from. An answer with no
citations (while `answer.citations: required`), or the model's "I can't answer from these"
marker, turns into a clarifying question instead.

**Streaming without showing unchecked text as final.** Guards (PII redaction, the prompt-leak
check, optionally groundedness) need the whole answer. Waiting for it would make users stare at
a blank screen, so with `stream_mode: draft_then_final` (the default):

- the words go out as **`delta`** events while the model writes them: a **draft**;
- exactly one **terminal** event follows, carrying the checked answer: **`final`** (the drafts
  were right), **`replace`** (a check changed the text: swap the draft for it), **`refusal`**
  (blocked: discard the draft) or **`error`**.

Drafts are already PII-redacted, and the abstain marker `INSUFFICIENT_CONTEXT` is never sent.
With `stream_mode: buffered` only the terminal event is sent, for clients that must never show
unchecked text, even briefly.

A real stream on the offline profile (the API sends exactly these lines):

```text
event: delta
data: {"text": "After"}

event: delta
data: {"text": " 30"}

event: delta
data: {"text": " days,"}

event: delta
data: {"text": " annual plans"}

event: delta
data: {"text": " are"}

event: delta
data: {"text": " not refundable, but you can cancel to stop the next renewal. [1]"}

event: final
data: {"answer": "After 30 days, annual plans are not refundable, but you can cancel to stop the next renewal. [1]", "citations": [{"marker": 1, "chunk_id": "b013db94be80a9c8e28b678eb1a179f7", "document_id": "samples:refund-policy.md", "title": "Refund policy", "uri": "file://refund-policy.md", "score": 0.2894, "snippet": "Refund policy > Annual plans Annual Plus and Team subscriptions can be refunded in full within 30 days of purchase. After 30 days, annual plans are not refundable, but you can cancel to stop the next renewal."}], "kind": "answer", "conversation_id": "8fe076c4e63540668802bb959f5cc7a2", "trace_id": "7330344975b9482cbdb7928709939b94", "usage": {"prompt_tokens": 554, "completion_tokens": 24, "embedding_tokens": 11, "cost_usd": null}, "warnings": []}
```

## How it works

[`SimpleOrchestrator.answer(state)`](../../src/chatbot/orchestration/simple.py), after
retrieval was judged strong:

1. **Sources:** the first `answer.max_context_chunks` (6) usable passages, rendered by
   `format_sources` as `[n] (Title > Heading, page N) text`.
2. **Prompt:** `answer.prompt` (`answer/v2`) rendered with the question, sources, history and
   memory ([prompts](prompts.md)).
3. **Generate** (`_generate`):
   - not streaming, or `buffered`: one `llm.generate` call;
   - `draft_then_final`: `_stream_drafts` iterates `llm.stream`. Each piece goes through a
     `StreamRedactor` that holds back the last `stream_holdback_chars` (64) characters, so an
     email address split across two pieces is still redacted whole. While the text so far
     could still become the abstain marker ("INSUFF…"), nothing is sent (`_could_become`).
     Each released piece is a `delta` event and is added to `state.drafted`.
4. **Citations:** `align(raw, sources)` keeps `[n]` markers that point at a real source (1 to
   the number of sources), drops the rest, tidies spaces, and returns the citations in marker
   order.
5. **Route:** the model abstained (the marker is in the text) or cited nothing while citations
   are required → `clarify`; groundedness check on → `check_grounded`; otherwise →
   `check_output`.

Then `check_output` runs the **output guards** on the complete text (PII redaction, the
prompt-leak check), and `_finish` saves the turn (redacted) and builds the `ChatResponse`.
`clarify` streams a clarifying question the same way, from the `cheap` model.

Finally **`terminal(state)`** builds the one terminal event:

| Situation | Event |
| --- | --- |
| `kind` is `refuse` (input or output blocked, or groundedness with `on_fail: refuse`) | `refusal` |
| Streaming, and the drafts differ from the final text (a dropped citation, redaction, a clarification replacing the answer) | `replace` |
| Otherwise | `final` |
| Anything raised on the way (`stream` catches it) | `error`, with the same message a non-streamed request would get, and the trace ID |

In [`api/routes/chat.py`](../../src/chatbot/api/routes/chat.py), `chat` returns
`await orchestrator.run(req)` as JSON, or wraps `orchestrator.stream(req)` in a
`StreamingResponse` where `sse(event)` writes `event: <name>\ndata: <json>\n\n`. The headers
`Cache-Control: no-cache` and `X-Accel-Buffering: no` stop proxies from buffering the stream.

## Inputs and outputs

**In:** the strong passages, the question, history and memory.

**Out:** a `ChatResponse`:

| Field | Example |
| --- | --- |
| `answer` | `After 30 days, annual plans are not refundable, but you can cancel to stop the next renewal. [1]` |
| `citations` | `[{"marker": 1, "chunk_id": "b013…", "document_id": "samples:refund-policy.md", "title": "Refund policy", "uri": "file://refund-policy.md", "score": 0.2894, "snippet": "…"}]` |
| `kind` | `answer`, `clarify` or `refuse` |
| `conversation_id`, `trace_id` | Pass the first back to continue; search logs by the second. |
| `usage` | Tokens and cost for the whole turn. |
| `warnings` | e.g. `pii:EMAIL`, `rerank_unavailable`, `groundedness:low`. |

`snippet` is the first 240 characters of the passage; `score` is its final score
(cosine, rerank, or fused). The full field list is in the [HTTP API](../api.md#post-v1chat).

## Turn it on, change it, turn it off

```bash
curl -N localhost:8000/v1/chat -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "How long is the refund window?", "stream": true}'
uv run chatbot ask --stream "How long is the refund window?"   # the same from the terminal
```

| Setting | Default | Notes |
| --- | --- | --- |
| `answer.prompt` | `answer/v2` | [Prompts](prompts.md). |
| `answer.max_context_chunks` | 6 | Passages in the prompt. More context, more tokens. |
| `answer.citations` | `required` | `optional`: uncited answers are allowed through. |
| `answer.on_weak_retrieval` | `clarify` | `refuse`: a fixed sentence instead ([weak retrieval](weak-retrieval-and-calibration.md)). |
| `answer.stream_mode` | `draft_then_final` | `buffered`: streams carry only the terminal event. |
| `guardrails.stream_holdback_chars` | 64 | Characters held back for PII redaction; longer than the longest PII you expect. |
| `guardrails.output.*` | redaction and prompt-leak check on, groundedness off | The checks behind `replace` and `refusal`. |

**Client rules** (any language): append `delta` texts to a draft; when a terminal event
arrives, show its `answer` (for `final` it equals the draft; for `replace` it differs; for
`refusal` discard the draft); show citations from the terminal event only. A complete browser
client is in the [HTTP API docs](../api.md#streaming-contract-adr-014).

**Turning streaming off:** don't ask for it (`stream: false`, the default), or set
`stream_mode: buffered` to keep the SSE shape without drafts.

## Extend it

- **Rich citations:** `align` builds each `Citation` from passage metadata (`title`, `uri`).
  Add metadata at ingest time (a page URL, a section anchor) and add a field to `Citation`
  in [`core/types.py`](../../src/chatbot/core/types.py) to pass it through.
- **Another citation style** (footnotes, quotes): change the answer prompt *and* `align`
  together; `align` is the only code that reads the markers.
- **Another output check:** add an output-stage guard ([extending: add a guard](../extending.md#add-a-guard)).
  It sees the complete text in every mode; returning `block` produces a `refusal`, changing the
  text produces a `replace`.
- **Another transport** (WebSocket, a queue): iterate `orchestrator.stream(req)` yourself; each
  `ChatEvent` has `event`, `data` and `terminal`. Keep the rule that exactly one terminal event
  ends a turn.

## Test it

```bash
uv run pytest tests/unit/test_orchestrator.py tests/unit/test_retrieval_citations.py tests/unit/test_api.py
```

| Test file | Covers |
| --- | --- |
| [`test_orchestrator.py`](../../tests/unit/test_orchestrator.py) | answers with citations; `final` when the draft matches; `replace` when the output changes or an uncited answer becomes a clarification; the abstain marker is never streamed; `error` events; buffered mode; PII redacted in drafts and final; prompt leaks blocked after streaming |
| [`test_retrieval_citations.py`](../../tests/unit/test_retrieval_citations.py) | `align` keeps valid markers only; sources are numbered from 1 |
| [`test_api.py`](../../tests/unit/test_api.py) | the SSE contract over HTTP |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Most answers end as `replace` with a clarifying question | The model doesn't cite (`citations: required`) | A model that follows the citation rule, or keep the `[n]` instruction in the prompt; `citations: optional` as a last resort. |
| The stream arrives all at once | A proxy buffers it | Disable buffering for `/v1/chat` (nginx: `proxy_buffering off`; the app already sends `X-Accel-Buffering: no`). |
| The last words appear in one burst | The PII holdback releases them at the end | Expected; lower `stream_holdback_chars` only if your PII patterns are short. |
| The client shows the draft after a `refusal` | The client ignores terminal events | Follow the client rules above. |
| Citations point at the wrong passage | The model miscounted sources | Fewer `max_context_chunks`, a stronger model; `align` already drops out-of-range markers. |
| `error` event with "internal error" | An unexpected failure | Search logs by the `trace_id` in the event. |

## Related

- Before: [weak retrieval and calibration](weak-retrieval-and-calibration.md).
- The steps around it: [orchestration](orchestration.md).
- The wording: [prompts](prompts.md).
- The HTTP contract: [HTTP API](../api.md#streaming-contract-adr-014).
- Terms: [citation](../glossary.md#citation), [SSE](../glossary.md#sse),
  [draft / final](../glossary.md#draft--final-streaming), [guard](../glossary.md#guard).
