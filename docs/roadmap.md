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

## M4: local and alternative providers (this release)

Ollama chat and `nomic-embed-text` embeddings (`local-ollama` profile); Qdrant as a second
vector store with Postgres keeping the index catalog; `chatbot reindex` builds a new physical
index beside the live one, `chatbot index activate` flips the alias and `rollback` flips it
back; SQL record source; pgvector operations guide (filtered search tuning, autovacuum,
`REINDEX INDEX CONCURRENTLY`); Compose profiles for Ollama and Qdrant.

## M5: production extras

OpenTelemetry, Langfuse, LangGraph orchestrator adapter, Kubernetes example, optional GraphQL,
typed TypeScript client generated from the OpenAPI spec, docs site.
