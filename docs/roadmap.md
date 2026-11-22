# Roadmap

Each milestone ends in something runnable. Order and scope follow the architecture plan; no dates.

## M1: end-to-end demo (done)

Ingest sample documents, ask, get a cited answer or a clarifying question. OpenAI + pgvector +
dense retrieval, API keys, Docker Compose, Postgres job queue, heuristic PII and injection
guards, streaming contract, per-user deletion, unit/integration/e2e tests and CI.

## M2: safety and memory (done)

Detector chains for PII (regex, optional Presidio) and injection (hardened heuristics, optional
model classifier); prompt-leak check on the final text in every stream mode; rolling
conversation summaries and long-term user facts, both redacted before storage and covered by
`forget_user`; JWT/OIDC auth alongside API keys.

## M3: retrieval quality and evals (done)

BM25 in Postgres, hybrid retrieval with reciprocal rank fusion, HyDE, query rewriting for
follow-ups, rerank (Cohere, local cross-encoder); a gate score so thresholds survive fusion;
`chatbot calibrate` storing `min_score` + `min_hits` per index and retrieval pipeline;
`chatbot eval` with retrieval, answer, faithfulness and abstention metrics; an offline CI gate
on every pull request and a nightly live run; optional groundedness check on answers.

## M4: local and alternative providers (done)

Ollama chat and `nomic-embed-text` embeddings (`local-ollama` profile); Qdrant as a second
vector store with Postgres keeping the index catalog; `chatbot reindex` builds a new physical
index beside the live one, `chatbot index activate` flips the alias and `rollback` flips it
back; SQL record source; pgvector operations guide (filtered search tuning, autovacuum,
`REINDEX INDEX CONCURRENTLY`); Compose profiles for Ollama and Qdrant.

## M5: production extras (this release)

OpenTelemetry traces and metrics behind a no-op-by-default facade; Langfuse over its OTLP
endpoint; token and cost accounting per chat turn and job (`usage_records`, `GET /v1/usage`,
`chatbot usage`); LangGraph as an optional orchestrator running the same steps as the default;
`/livez` and `/readyz`; a Kubernetes example (Kustomize base, dev and prod overlays, an
OpenTelemetry Collector component); a Compose profile with a Collector and Jaeger.

## Open, not scheduled

From the architecture plan, not built yet: an optional GraphQL API beside REST, a typed
TypeScript client generated from the OpenAPI spec, and a docs site. Other gaps worth a
milestone: per-user rate limits and budgets (the usage records make them possible), retention
for turns and usage records, and a Helm chart for teams that prefer one.
