# Roadmap

Each milestone ends in something runnable. Order and scope follow the architecture plan; no dates.

## M1: end-to-end demo (this release)

Ingest sample documents, ask, get a cited answer or a clarifying question. OpenAI + pgvector +
dense retrieval, API keys, Docker Compose, Postgres job queue, heuristic PII and injection
guards, streaming contract, per-user deletion, unit/integration/e2e tests and CI.

## M2: safety and memory

Presidio (or model-based) PII detection and model-based injection checks as options; rolling
conversation summaries and long-term user facts, both redacted before storage and covered by
`forget_user`; checks on the final text in streaming mode; JWT/OIDC auth.

## M3: retrieval quality and evals

BM25, hybrid (reciprocal rank fusion), HyDE, rerank (cross-encoder or Cohere); eval runner with
hit rate, faithfulness and abstention metrics; CI gate with cached responses on PRs and live
runs nightly; `make calibrate` to store `min_score` and `min_hits` per index; groundedness check.

## M4: local and alternative providers

Ollama LLM and `nomic-embed-text` embeddings; Qdrant store; re-index into a new physical index
and flip the alias; SQL record source; pgvector operations guide (filtered search tuning,
autovacuum, `REINDEX INDEX CONCURRENTLY`).

## M5: production extras

OpenTelemetry, Langfuse, LangGraph orchestrator adapter, Kubernetes example, optional GraphQL,
typed TypeScript client generated from the OpenAPI spec, docs site.
