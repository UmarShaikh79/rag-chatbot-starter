# Changelog

This is a template you copy and own (ADR 001). Each entry lists what changed and, when it
matters, how to bring the change into a copy you have already modified.

## Unreleased

- License changed from MIT to the **Apache License 2.0** (`LICENSE`, new `NOTICE`,
  `pyproject.toml`, README). Apache 2.0 is as permissive for users and adds an explicit patent
  grant from contributors. Copies taken before this change stay under MIT.
- README rewritten for first-time readers: real answers from the sample corpus (with citations,
  a clarifying question, PII redaction, an injection refusal, streaming), a zero-key quick start
  checked against `.env.example`, a feature overview, measured quality per setup, a diagram of
  the flow, use cases beyond support bots, client examples (curl, Python, browser streaming) and
  a docs map by goal.
- Readability pass for a mixed audience (no behaviour change): dense one-liners unfolded into
  named steps (the API-key versus JWT routing in `auth/api_key.py`, mixed `and`/`or`
  conditions, two nested conditional expressions, comprehensions with several loops); long
  functions split into named helpers (`SimpleOrchestrator._stream_drafts` and
  `_could_become`, Ollama's `_ThinkFilter`, `_terminal_kind` in `instrument.py`, the fake
  model's purpose → handler table); "why" comments where the idea is not obvious (`part()` in
  the container, BM25's formula, `StreamRedactor`, the pgvector filter SQL, context variables).
  Ruff now enforces complexity limits (`C901` 12, `PLR0912` 12, `PLR0915` 50). Checked against
  the old code: auth on 384 input combinations, BM25 on 400 random corpora, the fake model and
  the think filter on every purpose and 5,000 random streams, and the offline eval (identical
  metrics).
- Dependabot no longer proposes a new Python version for the Docker base image. Its first run
  proposed 3.12 -> 3.15, which CI passed (CI runs 3.12 and doesn't build the image) but which
  breaks the image with the `presidio`, `qdrant` and `langgraph` extras (no 3.15 wheels for
  `preshed`, `numpy` 2.4.6, `zstandard`). Move the Dockerfile and CI's `python-version`
  together, by hand.
- Ready to go public:
  - New `api` settings: `api.cors` lets browser apps on listed origins call the API directly
    (off by default; preflights are answered before auth, and error responses carry the CORS
    headers so the app sees a 401 rather than a CORS error); `api.docs` turns `/docs`, `/redoc`
    and `/openapi.json` off. ADR 017 described single-page apps calling the API with their
    token, which browsers blocked without CORS.
  - `CONTRIBUTING.md` (setup, checks, how to send a change, code style for a mixed audience),
    `SECURITY.md` (private vulnerability reporting), issue and pull request templates.
  - Workflows run with a read-only token (`permissions: contents: read`); Dependabot opens
    weekly updates for Python (uv), GitHub Actions and the Docker base image.
  In a copy you have modified: nothing to do; add `api.cors.allow_origins` if a browser app
  calls your API.
- Fix: known limitations from the testing pass.
  - Golden set: q17 ("Does Tidepool have a Linux desktop app?") was labelled unanswerable, but
    the docs list every platform the app runs on, so "No, not Linux" is grounded. It is now
    answerable (`samples:getting-started.md`), and q35 ("Is there a browser extension for
    clipping web pages?") keeps 10 unanswerable questions: 35 in all. The live OpenAI suite
    failed on q17. `evals/baselines/offline.json` re-measured (CI sequence on a fresh
    database).
  - `must_include` matched exact substrings, so correct rephrasings failed ("aren't refunded"
    for "not refunded", q02, q13, q25). Answers and phrases are now compared after normalizing
    case, quotes, dashes, markdown and contractions (`evals.metrics.normalize`), and an entry
    may be a list of alternatives, any one of which counts.
  - `chatbot index unalias NAME` removes an alias and its rollback pointer, keeping the indexes
    for `index drop`; this needed SQL before. Refuses the configured alias without `--force`.
    `CatalogStore.remove_alias` (pgvector and Qdrant); `IndexAdmin` gains `remove_alias`.
  - `guardrails.pii.allow` ships with the sample product's names (`Tidepool`, `Tidepool
    Notes`): Presidio tagged them as people and dropped them from questions. Replace them with
    your own product, feature and integration names.
  - Rerank cost: reranker profiles take `price_per_ksearch` or `price_per_mtok.input`. Cohere
    calls are priced from the billed units in each response into `usage.cost_usd` and the
    usage record (a `rerank` call, `kind: rerank`); `priced_mixed` no longer overwrote a cost
    already priced. Cohere's prices ship `null` (not published on its pricing page).
  In a copy you have modified: golden questions may now use lists in `must_include`; a custom
  `IndexAdmin` store needs `remove_alias`; set your reranker's price in `config/models.yaml`.
- Fix: error texts that leave the process kept credentials found in the error. A job whose
  OpenAI key was wrong stored `Incorrect API key provided: sk-inval****…wxy` in `last_error`,
  returned by `GET /v1/jobs/{id}`, and the same text went into the span status and exception
  event exported to Jaeger or Langfuse. New `scrub_secrets()` and `error_text()` in
  `core/errors.py` replace provider keys (`sk-`, `sk-proj-`, `pk-lf-`/`sk-lf-`, masked or not),
  `cb_` API keys, JWTs, bearer tokens, URL passwords and `api_key=`/`token=`/`password=` values
  with `[redacted]`, keeping the rest of the message. Used for job `last_error`, ingest and
  reindex `failed` entries, eval report errors, span status and exception events, and the
  `detail` of 500/502 responses. Log lines are unchanged. In a copy you have modified: use
  `error_text(exc)` wherever you store or export an error.
- `.env.example` lists every variable the template reads, all without values: examples moved
  into the comments, so a copied file never carries a placeholder such as a sample database
  URL. Only `CHATBOT_PROFILE`, `OPENAI_API_KEY`, `CHATBOT_API_KEYS` and `DATABASE_URL` are
  uncommented (an empty profile means `dev`); every optional feature's variables are commented
  until used. Added what was missing: `OTEL_RESOURCE_ATTRIBUTES`, `OLLAMA_API_KEY` (example
  name), `CHATBOT__AUTH__JWT__HS256_SECRET_ENV`, `CHATBOT__GUARDRAILS__PII__DETECTORS`,
  `CHATBOT_ROOT`, and the test switches `TEST_DATABASE_URL`, `QDRANT_TEST_URL`,
  `OLLAMA_TEST_URL`, `RUN_LIVE_TESTS`. `tests/unit/test_env_example.py` now also finds
  `os.environ` reads in `src` and `tests`, and fails on any value or extra uncommented line.
  In a copy you have modified: compare your `.env` with the new file; nothing in it changes.
- Fix: a missing `OPENAI_API_KEY` crashed `chatbot serve`, `chatbot worker` and the CLI with the
  OpenAI SDK's 20-line traceback ("Missing credentials"). Missing keys are now one message
  before anything starts, e.g. `Missing API key(s) for the 'dev' profile: OPENAI_API_KEY needed
  by models.answer (openai:answer), models.cheap (...), models.embed (...)`, exit code 2
  (`Settings.required_credentials()` / `check_credentials()`; the Cohere key counts when
  reranking is on). `chatbot serve` also checks `DATABASE_URL`, the API-key hashes or JWT secret
  and the Langfuse keys before uvicorn starts; those errors are now `ConfigError` (still a
  `RuntimeError`) instead of bare `RuntimeError`s that the CLI printed as tracebacks. The OpenAI
  adapters raise the same `ConfigError` when built without a key.
- Fix: background jobs retried errors that can't succeed. A key without access to the model
  (403), a wrong key (401), a missing model or source (404) or a bad request was retried up to
  `worker.max_attempts` times with backoff before failing. The worker now asks
  `is_permanent(exc)` (`core/errors.py`: 4xx other than 408/409/425/429, from OpenAI, `httpx`,
  Ollama or Cohere errors, following `raise ... from` causes; template errors marked
  `permanent`) and fails such jobs on the first attempt. Rate limits, 5xx, timeouts and
  connection errors are retried as before. `ProviderError` now carries the upstream
  `status_code`. In a copy you have modified: a custom `JobQueue` needs the new
  `retry: bool = True` parameter on `fail()`.
- Fix: a non-streamed chat whose model or embedding provider failed (OpenAI down, a bad key,
  Ollama without the model) answered Starlette's plain-text `500 Internal Server Error`, with no
  `trace_id`, unlike every other error. Failures now answer JSON `{"detail", "trace_id"}` with
  `X-Trace-Id`: 502 when a provider failed (`ProviderError`, which `OllamaError` and
  `RerankError` now extend, and `httpx`/OpenAI SDK errors), 500 for the template's own errors
  and, from `TraceMiddleware`, anything unexpected (`internal error`). SDK messages are not
  shown, since an OpenAI 401 repeats part of the key. Streams report the same message
  (`describe_error` in `core/errors.py`). In a copy you have modified: take `describe_error`,
  the `_failed` handler in `api/app.py` and the `except` block in `TraceMiddleware`.
- Fix: a reranker failure took every chat request down. With a Cohere trial key (10 calls a
  minute) or during a Cohere outage, `/v1/chat` answered 500 for everyone. Now:
  - the Cohere reranker retries 429, 5xx, timeouts and connection errors (twice within about
    1.5 s in a chat turn; it never retried before) and respects `Retry-After`;
  - if it still fails, the turn is answered from the retrieval order, gated by the retriever's
    own calibration, with the warning `rerank_unavailable` (new setting
    `retrieval.rerank.fallback`, default true; `false` keeps the old failure);
  - `chatbot calibrate` and `chatbot eval` back off for up to about 3 minutes and never fall
    back, so they finish on a trial key instead of aborting with a traceback;
  - reranking has its own `rerank` span (model, candidates, fallback), the Cohere call a child
    span with attempts and billed units, and new metrics `chatbot.rerank.retries` and
    `chatbot.rerank.fallbacks`.
  In a copy you have modified: take `providers/rerankers/`, `RetrievalPipeline.retrieve` and
  `used()`, the `used` lines in `SimpleOrchestrator.retrieve`, `gate_signal(rerank=...)` and the
  `patient_reranking()` blocks in `evals/commands.py`. A custom reranker gets the fallback for
  free; raise any exception to trigger it.
- Fix: with JWT auth, an identity-provider outage while no signing keys were cached (for
  example an API restart during the outage) answered valid tokens with 401, which many front
  ends treat as "sign out". It is now 503 with `Retry-After: 30` (`KeysUnavailable` in
  `auth/jwt.py`, handled in `auth/api_key.py`). Bad tokens are still 401.
- Fix: errors raised as `HTTPException` (auth 401/403/503, route 403/404) returned
  `{"detail": ...}` without the `trace_id` that `docs/api.md` promises; `api/app.py` now
  handles them like the other errors, keeping their headers.
- Fix: on the first turn of a conversation only the root span carried the session
  (`langfuse.session.id`), so Langfuse filters by session missed that turn's steps.
  `MeteredOrchestrator` now assigns the conversation ID before the turn's first span. A
  custom orchestrator now receives a `conversation_id` on first turns too;
  `chatbot.conversation.new` on the root span still says whether it is new.
- Docs: rotating keys in Keycloak without the 30 s refetch limit failing tokens; user and
  session IDs are on every OpenTelemetry span, whatever `capture_text` says; running
  Langfuse's Compose file next to this one. First live runs against Keycloak 26.4 and
  self-hosted Langfuse 4.83 recorded in ADR 017 and ADR 027.
- Fix: the Kubernetes prod overlay's ingress host placeholder `chatbot.YOUR_DOMAIN` is not a
  valid host name, so `kubectl apply -k deploy/k8s/overlays/prod --dry-run=server` (the
  README's own check) always failed; kubeconform in CI does not check host values. The
  placeholder is now `chatbot.example.com`, the README creates the namespace before the dry
  run, and `tests/unit/test_k8s_manifests.py` checks ingress hosts. The dev overlay has now
  been run on kind (Kubernetes 1.37). In a copy you have modified: nothing to do if you already
  set your own host.
- Fix: with OpenTelemetry on, FastAPI 0.165+ added its own request telemetry next to the
  template's. Every request became two traces (the second an empty root), was counted twice in
  `http.server.request.duration`, and every health check became a trace (22 in 10 minutes from
  the Compose healthcheck alone). The app now turns FastAPI's request spans and metrics off and
  skips probes (`api/app.py`, `_fastapi_telemetry`; older FastAPI has nothing to turn off).
  In a copy you have modified: take `_fastapi_telemetry` and its use in `create_app`.
- Fix: Presidio redacted answers built from your own documents ("Tidepool Notes is..." became
  "[PERSON] is...", "choose Request refund" became "choose [PERSON] refund"). New setting
  `guardrails.pii.output_detectors` (default `[regex]`) is used for answers: the output guard,
  the streamed drafts and the stored answer, which now always agree. `guardrails.pii.detectors`
  still covers user messages, stored questions, summaries and facts. Nothing changes unless you
  had turned Presidio on; `output_detectors: null` restores one redactor for both.
  `docs/configuration.md` now says what Presidio catches and misses, its cost, and how to pass
  the spaCy model to a Compose build.
- Fix: questions with `filters` were judged weak against a `min_hits` calibrated on the whole
  corpus. A filter to one document (one customer, one product) rarely has two strong chunks,
  so with a calibrated `min_hits` of 2 such questions always got a clarifying question, even
  with a strong match. New setting `retrieval.min_hits_filtered` (default 1) applies to
  filtered requests, and `min_hits` is never more than the number of chunks retrieval
  returned. `min_score` is unchanged. In a copy you have modified: take the `assess` change in
  `retrieval/weak.py`, the two new arguments at its call in `orchestration/simple.py`, and the
  setting; set `min_hits_filtered: null` to keep the old behaviour.
- Fix: metadata filters meant different things per vector store. A single value on a list
  field (`{"tags": "contact"}` against `["hours", "contact"]`) matched in Qdrant, never in
  pgvector (JSON containment does not match a value against a nested array) and only by
  equality in the in-memory test store. All stores now follow Qdrant's documented rule,
  written down as `chatbot.core.interfaces.metadata_matches`: a single value matches the value
  or any element of a list; a list requires every element. pgvector's clause is
  `metadata @> {k: v} OR metadata @> {k: [v]}` per value, still served by the GIN index and
  still compatible with HNSW. Filters that worked before return the same results.
  `tests/integration/test_store_filters.py` runs the same cases against every store. In a copy
  you have modified: take `PgVectorStore._where`; a custom store should apply
  `metadata_matches` and join that test.
- Fix: the Docker image now includes `evals/`. Before, `chatbot calibrate` and `chatbot eval`
  failed inside a container (`FileNotFoundError: /app/evals/datasets/golden.jsonl`), so an
  image-only deployment (Compose without uv on the host, Kubernetes) could not calibrate its
  thresholds. A unit test checks that the Dockerfile copies every folder the app reads at
  runtime. In a copy you have modified: add `COPY evals ./evals` to `docker/Dockerfile`.
- Fix: clarifying questions were refused as prompt leaks. `clarify/v1` puts the knowledge-base
  topics into the system prompt and asks the model to mention them; the prompt-leak check
  compared answers with the rendered system prompt, so a model that listed the topics (every
  time with `gemma3:4b`) got "Sorry, I can't help with that request." The check now compares
  with the template's own wording only (`PromptTemplate.system_literals()`, passed to the guard
  as `system_prompt_parts`); repeating the instructions is still refused. In a copy you have
  modified: take `system_literals`, the `PromptLeakGuard.check` change and the extra
  `_check_output` argument; custom guards that read `system_prompt` keep working.
- Fix: `chatbot eval --gate` for a profile with no entry in `evals/gate.yaml` ran the whole
  eval and then crashed before writing the report. It now fails at once, and the report
  (`--out`) is written before the gate is checked, so a failing run is never lost.
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
