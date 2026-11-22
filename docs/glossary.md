# Glossary

The words used across these docs and the code, in plain language, with a pointer to where
each one lives. Entries are in alphabetical order; every guide links here instead of
re-explaining a term.

Back to the [docs home](README.md).

---

### Abstain

What the model is told to do when the passages it was given don't answer the question: reply
with a fixed marker, `INSUFFICIENT_CONTEXT` (set in `prompts/answer/v2.yaml`), instead of an
answer. The orchestrator turns that into a [clarifying question](#clarifying-question). See
[`SimpleOrchestrator.answer`](../src/chatbot/orchestration/simple.py).

### Alias

A stable name (for example `docs`) that points at one physical [index](#index). Code and
config only ever use the alias, so a new index can be built beside the live one and the alias
switched in one step. See [re-indexing and aliases](features/reindexing.md).

### API key

A secret string a trusted backend sends in `X-API-Key` (or `Authorization: Bearer`). Only its
SHA-256 hash is stored, in `CHATBOT_API_KEYS`. Create one with `make api-key`. See
[configuration: `auth`](configuration.md#auth).

### BM25

A classic keyword-ranking formula ("Okapi BM25"). It scores a passage by how often the
question's words appear in it, how rare those words are across all passages, and how long the
passage is. Good at exact terms that [embeddings](#embedding) blur: error codes, product names,
IDs. See [`bm25_rank`](../src/chatbot/retrieval/bm25.py) (the formula is explained in its
docstring) and ADR [020](adr/020-bm25-in-postgres.md).

### Calibration

Choosing [`min_score`](#min_score-and-min_hits) and `min_hits` from data instead of guessing:
`chatbot calibrate` runs the [golden set](#golden-set), tries every useful threshold, and
keeps the one that answers the answerable questions while declining the rest. Thresholds are
stored per [index](#index) and [scorer](#scorer). See [evals: calibration](../evals/README.md#calibration)
and ADR [011](adr/011-retrieval-thresholds-are-calibrated-never-constants.md).

### Chunk

A passage of a document small enough to embed and to show a model: about 512 tokens by
default, with a breadcrumb of the document title and section heading on its first line. See
[chunking](features/chunking.md).

### Chunker version

A string naming the chunker and its settings (for example `recursive-v1:512:64:approx`). It is
stored with every document, so changing the settings re-chunks documents on the next sync.

### Citation

A numbered reference (`[1]`) in an answer, pointing at the passage it came from. The response
lists each citation with its document, title and score.

### Clarifying question

What the bot asks instead of answering when retrieval is too weak or the model
[abstains](#abstain): "Could you tell me a bit more...". Never an invented answer
(ADR [006](adr/006-weak-retrieval-asks-never-invents.md)).

### Container

The one function that builds every part from settings:
[`container.build`](../src/chatbot/core/container.py). It reads names from config, asks the
[registry](#registry) for the matching factory, and wires the parts together. Tests pass fakes
to it. See [architecture](architecture.md#3-the-container-builds-everything).

### Content hash

A SHA-256 fingerprint of a document's bytes. Ingestion skips a document whose hash,
[chunker version](#chunker-version) and embedding model are unchanged.

### ContextVar

A Python variable with its own value per request (per asyncio task), so concurrent requests
never see each other's trace, user or usage ledger. See the comment above `_trace_attrs` in
[`tracing.py`](../src/chatbot/observability/tracing.py).

### Cosine similarity

How close two [embeddings](#embedding) point in the same direction, from -1 to 1 (usually 0 to
1 for text). The dense retriever's score. pgvector computes it with the `<=>` operator.

### Dense retrieval

Finding passages by meaning: embed the question, then fetch the passages whose embeddings are
closest ([cosine similarity](#cosine-similarity)). The default strategy. See
[`DenseRetriever`](../src/chatbot/retrieval/dense.py).

### Draft / final (streaming)

With `answer.stream_mode: draft_then_final`, words are sent as `delta` events while the model
writes, then one `final` (or `replace`) event carries the checked answer. See
[api: streaming contract](api.md#streaming-contract-adr-014).

### Embedding

A list of numbers (a vector, 1536 of them for `text-embedding-3-small`) that captures the
meaning of a text. Similar meanings give nearby vectors. Made by an embedder
([`Embedder`](../src/chatbot/core/interfaces.py) Protocol).

### Eval

A scored run of the [golden set](#golden-set): does retrieval find the right documents, does
the answer contain the expected facts, does it decline unanswerable questions. CI fails a pull
request that makes scores drop. See [evals](../evals/README.md).

### Extra

An optional group of packages installed with `uv sync --extra <name>`: `otel`, `langfuse`,
`langgraph`, `qdrant`, `rerank`, `presidio`. The base install works without any of them.

### Factory

A function that builds a part, registered under a name:
`@registry.register("retriever", "dense") def make_dense(...)`. See [registry](#registry).

### Gate score

The single score [`min_score`](#min_score-and-min_hits) is compared against: dense, BM25 or
rerank, set by `retrieval.gate` (`auto` picks it). Never a fused [RRF](#rrf) rank, which has no
meaning as a threshold. See ADR [019](adr/019-hybrid-retrieval-with-rank-fusion-and-a-gate-score.md).

### Golden set

The questions the bot is measured on, with the documents and phrases a correct answer needs,
in `evals/datasets/golden.jsonl`. Replace it with questions about your own documents.

### Groundedness

Whether every claim in an answer is supported by the passages it cites. An optional output
check (`guardrails.output.groundedness_check`) and the eval's faithfulness metric.

### Guard

A check that runs on text at a stage (input, context, output) and allows, modifies (redacts)
or blocks it. See [`Guard`](../src/chatbot/core/interfaces.py) and ADR
[007](adr/007-safety-on-by-default.md).

### HNSW

"Hierarchical navigable small world": the graph index pgvector and Qdrant use to find nearby
vectors fast without comparing against every one. Approximate: very fast, very slightly less
exact. See [operating pgvector](operations/pgvector.md).

### Hybrid retrieval

Running dense and BM25 retrieval together and merging their lists with [RRF](#rrf), so a
passage that matches by meaning *or* by exact keyword can win.

### HyDE

"Hypothetical document embeddings": ask a model to write a short imagined answer, and search
with that instead of (or as well as) the question. Helps short or vague questions.

### Index

A physical set of stored chunks and vectors, built for one embedding model, one
[chunker version](#chunker-version) and one store (ADR
[005](adr/005-the-embedding-model-is-part-of-the-index.md)). Reached through an [alias](#alias).

### Ingestion

Getting documents in: read from a [source](#source), [parse](#parser), [chunk](#chunk),
[embed](#embedding), store. See [sources and parsers](features/sources-and-parsers.md).

### Job

A unit of background work in the Postgres queue (`sync_source`, `ingest_documents`,
`memory_refresh`), run by the [worker](#worker). See [background jobs](features/background-jobs.md).

### JWT / OIDC

A JWT is a signed token your identity provider (Auth0, Okta, Keycloak, Entra ID ...) gives a
signed-in user; OIDC is the standard for finding the provider's signing keys. The API checks
the signature, issuer, audience and expiry, and treats the token's subject as the user. See
[configuration: `auth`](configuration.md#auth) and ADR [017](adr/017-jwt-oidc-alongside-api-keys.md).

### `min_score` and `min_hits`

The two thresholds that decide whether retrieval is strong enough to answer: at least
`min_hits` passages with a [gate score](#gate-score) of at least `min_score`. Always
[calibrated](#calibration).

### Orchestrator

The code that runs one chat turn step by step: guards, memory, retrieval, answer, checks. See
[`SimpleOrchestrator`](../src/chatbot/orchestration/simple.py) and
[architecture](architecture.md#4-one-chat-request-from-start-to-finish).

### Parser

Turns a file's bytes (Markdown, HTML, PDF, DOCX, text) into clean text split into sections.
See [sources and parsers](features/sources-and-parsers.md).

### PII

Personally identifiable information: emails, phone numbers, card numbers, names. Redacted to
`[EMAIL]`, `[CARD]` ... before it is stored or sent to a model.

### Profile

A named set of settings in `config/profiles/` chosen with `CHATBOT_PROFILE`: `dev` (OpenAI),
`offline` (no network), `local-ollama`, `prod`. See [configuration: profiles](configuration.md#profiles).

### Prompt injection

Text that tries to override the bot's instructions ("ignore previous instructions..."), in a
user message or hidden in a document. Detected and refused; see ADR
[018](adr/018-layered-pii-and-injection-detectors.md).

### Protocol

A Python `typing.Protocol`: the list of methods a part must have, with no base class to
inherit. Any class with those methods fits. All of them are in
[`core/interfaces.py`](../src/chatbot/core/interfaces.py). See
[architecture](architecture.md#1-parts-are-protocols).

### RAG

Retrieval-augmented generation: find the passages that answer a question, then have a
language model write the answer from those passages only, citing them.

### Registry

The table that maps a config name to a [factory](#factory): config says
`retrieval.strategy: dense`, the registry finds the function registered as
`("retriever", "dense")`. See [`core/registry.py`](../src/chatbot/core/registry.py) and
[architecture](architecture.md#2-the-registry-turns-names-into-parts).

### Rerank

Re-scoring the top retrieved passages with a model that reads the question and each passage
together (Cohere, a local cross-encoder). Slower than retrieval, more accurate.

### RRF

Reciprocal rank fusion: merge several ranked lists by adding `1 / (k + rank)` for each list a
passage appears in. Uses ranks, not scores, so lists with incomparable scores can be combined.

### Scorer

The calibration key for a retrieval pipeline: the [gate](#gate-score) plus every step that
shapes the score, for example `dense|dense` or `rerank|rerank[rerank-v5.0-fast](dense)`.
Change any step and the thresholds must be recalibrated.

### Source

Where documents come from: a folder of files, a SQL query, or documents pushed through the
API. See [sources and parsers](features/sources-and-parsers.md).

### SSE

Server-Sent Events: an HTTP response that stays open and sends `event:` / `data:` blocks as
they happen. How answers stream.

### Trace ID

An ID given to every request and job, returned in the `X-Trace-Id` header and every error, and
written on every log line and span, so one request can be followed everywhere. See
[observability: trace IDs](observability.md#trace-ids).

### Worker

The background process (`chatbot worker`) that takes [jobs](#job) from the queue and runs them.
See [background jobs](features/background-jobs.md).
