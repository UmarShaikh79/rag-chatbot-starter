# Changelog

This is a template you copy and own (ADR 001). Each entry lists what changed and, when it
matters, how to bring the change into a copy you have already modified.

## Unreleased

- Fix: the Docker image now includes `evals/`. Before, `chatbot calibrate` and `chatbot eval`
  failed inside a container (`FileNotFoundError: /app/evals/datasets/golden.jsonl`), so an
  image-only deployment (Compose without uv on the host, Kubernetes) could not calibrate its
  thresholds. A unit test checks that the Dockerfile copies every folder the app reads at
  runtime. In a copy you have modified: add `COPY evals ./evals` to `docker/Dockerfile`.
- First live run of the `dev` profile (OpenAI). The model IDs `gpt-6.2-sol` and `gpt-6-luna`
  are confirmed, and `config/models.yaml` now has prices (standard tier, 2026-11-22), so
  `cost_usd` is filled in. `evals/baselines/dev.json` records the first measured run:
  hit@3 1.0, answer rate 0.96, faithfulness 1.0, unanswerable rate 0.9, about $0.03 per run.
  Calibrated thresholds for `text-embedding-3-small` on the sample corpus:
  `min_score 0.409`, `min_hits 1`. In a copy you have modified: copy the prices, and record
  your own baseline with `chatbot eval --update-baseline` on your corpus.
- First live run of the `local-ollama` profile (Ollama 0.40.17 on an Apple M1, 16 GB).
  `evals/gate.yaml` gains a `local-ollama` gate (same floors as `dev`), and
  `evals/baselines/local-ollama.json` records the run: hit@1 1.0, answer rate 1.0,
  faithfulness 1.0, unanswerable rate 0.9, p50 latency about 22 s. `nomic-embed-text`
  calibrated to `min_score 0.676`, so the profile's provisional `min_score` goes from 0.5 to
  0.65. In a copy you have modified: raise `retrieval.min_score` in
  `config/profiles/local-ollama.yaml`, or run `chatbot calibrate --apply`.

## 0.5.0: milestone 5

Observability, cost accounting, an optional LangGraph orchestrator and a Kubernetes example.

- Tracing and metrics (ADR 026, `uv sync --extra otel`, `observability.otel.*`): a small facade
  (`span`, `count`, `observe` in `chatbot.observability.tracing`) that does nothing until a
  backend is on. Spans for each request (continuing a W3C `traceparent`), each chat turn and
  its steps, each model and embedding call (GenAI conventions, tokens, cost), each job.
  Metrics for turns, latency, tokens, cost, weak retrieval, guard blocks, jobs and HTTP.
  OTLP/HTTP to wherever `OTEL_EXPORTER_OTLP_*` points. With tracing on, `trace_id` is the
  OpenTelemetry trace ID. Text on spans is off by default and always the redacted text.
- Langfuse (ADR 027, `uv sync --extra langfuse`, `observability.langfuse.*`) through its OTLP
  endpoint, without the Langfuse SDK: turns as traces with user and session, generations with
  model, tokens and cost, typed retrieval, guard and evaluator steps.
- Token and cost accounting (ADR 028): models are wrapped once in the container, so every call
  is priced from `price_per_mtok` and recorded. Migration 0005 adds `usage_records` (one row
  per chat turn, job and `ingest --inline`, with a per-call breakdown); `GET /v1/usage` (admin)
  and `chatbot usage` report by day, user, model or operation. `forget_user` unlinks the user
  from their rows (`usage_records_anonymised`).
- LangGraph (ADR 029, `uv sync --extra langgraph`, `orchestrator: langgraph`): the turn is now
  a list of step methods on `SimpleOrchestrator` sharing a `TurnState`; the LangGraph adapter
  runs the same steps as graph nodes, with identical events and responses (tested). The
  `orchestrator` setting now selects from the registry (it was ignored before).
- Kubernetes example (ADR 030, `deploy/k8s`): Kustomize base (API, worker, nightly ingest
  CronJob, PDB), `dev` overlay with in-cluster pgvector and the offline profile, `prod`
  overlay with HPA, SSE-ready ingress and network policy, and an OpenTelemetry Collector
  component. CI renders and validates them with kubeconform.
- `/livez` (process only) and `/readyz` (database) beside `/healthz`; probes are no longer
  traced or access-logged.
- `chatbot migrate` takes a Postgres advisory lock, so concurrent runs migrate once.
- Compose profile `otel` (Collector + Jaeger, `make up-otel`); `make usage`, `make k8s-render`.
- CI: tests run with the `otel` and `langgraph` extras, a `test-base` job runs the unit tests
  with no extras, and a `k8s` job validates the manifests.
- `chatbot.__version__` now matches the package version.
- Guide: [docs/observability.md](docs/observability.md); ADRs 026-030.

Upgrading a copy of 0.4.0: run `chatbot migrate` (migration 0005 adds `usage_records`). If
you changed `SimpleOrchestrator._flow`, move your changes into the step method they belong to
(`check_input`, `retrieve`, `answer`, `clarify`, `check_grounded`, `check_output`); state that
was local to `_flow` now lives on `TurnState`. `container.build` wraps LLMs and embedders in
`MeteredLLM` / `MeteredEmbedder` and the orchestrator in `MeteredOrchestrator`; other attributes
pass through, and `chatbot.observability.instrument.unwrap` returns the original.
`Components` has a new `usage_store` field. `Worker` takes an optional `usage_store`.

## 0.4.0: milestone 4

Local and alternative providers, and safe re-indexing.

- Ollama adapters over its native API (ADR 025): chat with streaming, structured output,
  `keep_alive`, `num_ctx` and `think: false` (any `<think>` block is stripped), and
  embeddings with task prefixes. New `local-ollama` profile (answer, cheap and
  `nomic-embed-text`, alias `docs_local`); a missing model fails with the `ollama pull` to run.
- Qdrant as a second vector store (ADR 024, `uv sync --extra qdrant`,
  `vector_store.backend: qdrant`, `vector_store.qdrant.*`). Postgres keeps the catalog for
  every store (`chatbot.stores.catalog`); `forget_user` also deletes the user's Qdrant points.
- Re-indexing without downtime (ADR 023): `chatbot reindex` builds an index for the current
  configuration behind `<alias>_next` (resumable, with a catch-up pass), `chatbot index
  activate | rollback | drop | list`. Retrievers still refuse an index built with another
  embedding model, and the error now names the fix. Indexes can move between pgvector and
  Qdrant the same way.
- SQL record source (`type: sql`): one document per row from any SQLAlchemy database, streamed
  in batches, with metadata columns, owner column and templates.
- Compose profiles `ollama` (with a one-shot model pull) and `qdrant`; `make up-ollama`,
  `make up-qdrant`, `make reindex`, `make index-*`. CI runs the Qdrant tests against a real
  server too.
- Operations guides: [pgvector](docs/operations/pgvector.md) (query settings, index builds,
  autovacuum, `REINDEX INDEX CONCURRENTLY`, memory) and
  [re-indexing](docs/operations/reindexing.md).
- ADRs 023-025.

Upgrading a copy of 0.3.0: run `chatbot migrate` (migration 0004 adds `vector_indexes.backend`,
`status`, `ready_at`, `build_report`, `index_aliases.previous_index_id` and
`documents.sections`). Documents ingested before it keep no parsed text: sources re-read them
during a re-index, but documents pushed through the API must be pushed again (or left out with
`chatbot reindex --allow-missing`). If you changed `PgVectorStore`, note that its index and
calibration methods moved to the `CatalogStore` base class; `physical_name` is still
importable from `chatbot.stores.pgvector`. `IngestionPipeline.sync_source` takes an optional
`index`, and `ingest_parsed` is split out of `ingest_document`.

## 0.3.0: milestone 3

Retrieval quality and evals.

- Retrieval pipeline built from config (`retrieval.*`, ADR 019): `dense`, `bm25` (Okapi BM25
  in Postgres over a `tsvector` column, no extension, ADR 020) and `hybrid` (reciprocal rank
  fusion with optional weights); HyDE (`hyde/v1`) for chosen retrievers; query rewriting of
  follow-ups (`rewrite_query/v1`) used only for retrieval; rerank with Cohere (HTTP, no SDK),
  a local cross-encoder (`uv sync --extra rerank`) or a word-overlap reranker for tests.
- Every hit keeps its per-step scores (`ScoredChunk.scores`); weak-retrieval thresholds apply to
  a gate score (`retrieval.gate`), never to a fused rank.
- Calibration per index and scorer (ADR 021): `chatbot calibrate [--apply] [--check]` sweeps
  `min_score` with `min_hits` on the golden set; `GET /v1/sources` shows the active scorer and
  its thresholds.
- Evals (ADR 022): `chatbot eval` with hit@k, recall, MRR, answer rate, required phrases,
  citation precision, faithfulness and abstention; `--gate` against `evals/gate.yaml` and a
  committed baseline; CI `evals` job (offline) and a nightly `evals-live` workflow.
- Optional groundedness check on answers (`guardrails.output.groundedness_check`,
  `judge_groundedness/v1` or a lexical judge) with clarify, refuse or warn on failure.
- Golden set grows to 34 questions (10 unanswerable). Cohere reranker profile defaults to
  `rerank-v5.0-fast`.
- ADRs 019-022.

Upgrading a copy of 0.2.0: run `chatbot migrate` (adds `chunks.tsv`, `chunks.doc_len`,
`vector_indexes.text_config` and `retrieval_calibrations`, backfills existing chunks and moves
old thresholds to scorer `dense|dense`). `IndexInfo` lost `min_score`/`min_hits` (use
`get_calibration`). Custom retriever factories are now called with a `settings=` keyword:
add `**kwargs`. A custom vector store needs `keyword_search` for BM25 and
`get_calibration`/`save_calibration` for stored thresholds; without them those features are
unavailable and config thresholds apply.

## 0.2.0: milestone 2

Safety and memory.

- PII: detector chain (`guardrails.pii.detectors`) with the built-in `regex` detector and an
  optional Microsoft Presidio detector (`uv sync --extra presidio`) for names and places; an
  allow list; one configured redactor shared by guards, streaming, persistence and memory.
- Injection: heuristics now run on normalised text (NFKC, zero-width characters removed,
  leetspeak and spaced letters undone, base64 decoded) with new rules for role tags and headers,
  developer mode, link-based exfiltration and several languages; optional `llm` classifier
  (`guard_injection/v1`) with a fail-open or fail-closed setting.
- Output: prompt-leak check; output guards can now block, which ends a stream with `refusal`.
- Memory: rolling conversation summaries and long-term per-user facts (ADR 016), refreshed by a
  `memory_refresh` job or inline; facts are re-checked for PII and injection before storage;
  `GET /v1/users/{id}/memory` and `DELETE /v1/users/{id}/memory/facts/{fact_id}`;
  `forget_user` also deletes summaries and facts.
- Prompts: `answer/v2` (adds a memory section; now the default), `summarize/v1`,
  `extract_facts/v1`, `guard_injection/v1`.
- Auth: `auth.mode` gains `jwt` and `api_key_or_jwt` (ADR 017): OIDC discovery, JWKS caching
  and rotation, `iss`/`aud`/`exp` required, scopes, an admin scope; end users act only as
  their token's subject. `chatbot dev-token` signs local HS256 tokens.
- ADRs 016-018.

Upgrading a copy of 0.1.0: run `chatbot migrate` (adds `conversation_summaries` and
`user_facts`); if you customised the `MemoryStore`, add the new methods from
`core/interfaces.py`; if you replaced `prompts/answer/v1.yaml`, add a `{memory}` section or set
`answer.prompt` back to it; run the worker (`chatbot worker`) or set `memory.run_in: inline`.

## 0.1.0: milestone 1

First release: end-to-end RAG backend.

- Config loader (YAML + profiles + `CHATBOT__` env overrides) that rejects unknown keys.
- Protocols and a name registry for every swappable part; in-memory fakes for tests.
- OpenAI chat and embedding adapters; offline hashing embedder and extractive fake LLM.
- Ingestion of Markdown, text, HTML, PDF and DOCX files and of inline documents over the API;
  structure-aware recursive chunking; content-hash dedup; atomic per-document replace; removal
  of documents deleted at the source.
- Postgres job queue with retries, backoff, dedup keys and abandoned-job recovery; worker.
- pgvector store with per-index partial HNSW indexes, iterative scans for filtered search and an
  exact-scan path for selective filters; index records its embedding model.
- Dense retrieval with `min_score` + `min_hits` weak-retrieval detection; clarifying questions.
- Versioned prompts with strict rendering; inline `[n]` citations aligned to sources.
- Heuristic injection checks and PII redaction (input, retrieved context, output, storage);
  streaming redactor with a rolling buffer.
- SSE streaming with `draft_then_final` and `buffered` modes and one terminal event.
- Conversation memory per caller `user_id`; `DELETE /v1/users/{id}/memory`.
- API key auth, JSON logs with trace IDs, token and cost accounting.
- Docker Compose stack, GitHub Actions CI, sample corpus, 20 golden questions, ADRs 001-015.
