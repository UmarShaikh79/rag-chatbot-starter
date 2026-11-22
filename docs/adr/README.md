# Architecture decision records

| ADR | Title | Status |
| --- | --- | --- |
| [001](001-template-not-framework.md) | Template, not framework | Accepted |
| [002](002-python-tooling.md) | Python tooling | Accepted |
| [003](003-plain-python-orchestration.md) | Plain Python orchestration | Accepted (LangGraph adapter: ADR 029) |
| [004](004-postgres-for-everything-first.md) | Postgres for everything first | Accepted |
| [005](005-the-embedding-model-is-part-of-the-index.md) | The embedding model is part of the index | Accepted (re-index: ADR 023) |
| [006](006-weak-retrieval-asks-never-invents.md) | Weak retrieval asks, never invents | Accepted |
| [007](007-safety-on-by-default.md) | Safety on by default | Accepted |
| [008](008-memory-keyed-by-caller-user-id.md) | Memory keyed by caller user ID | Accepted |
| [009](009-hosted-and-local-defaults.md) | Hosted and local defaults | Accepted |
| [010](010-evals-as-a-ci-gate.md) | Evals as a CI gate | Accepted (implemented in milestone 3, ADR 022) |
| [011](011-retrieval-thresholds-are-calibrated-never-constants.md) | Retrieval thresholds are calibrated, never constants | Accepted (refined by ADR 021) |
| [012](012-filtered-vector-search-on-pgvector.md) | Filtered vector search on pgvector | Accepted |
| [013](013-re-ingesting-a-document-is-an-atomic-replace.md) | Re-ingesting a document is an atomic replace | Accepted |
| [014](014-streaming-with-output-guards.md) | Streaming with output guards | Accepted |
| [015](015-redact-before-persisting-delete-everything-per-user.md) | Redact before persisting; delete everything per user | Accepted |
| [016](016-rolling-summaries-and-long-term-facts.md) | Rolling summaries and long-term facts | Accepted (milestone 2) |
| [017](017-jwt-oidc-alongside-api-keys.md) | JWT/OIDC alongside API keys | Accepted (milestone 2) |
| [018](018-layered-pii-and-injection-detectors.md) | Layered PII and injection detectors | Accepted (milestone 2) |
| [019](019-hybrid-retrieval-with-rank-fusion-and-a-gate-score.md) | Hybrid retrieval with rank fusion and a gate score | Accepted (milestone 3) |
| [020](020-bm25-in-postgres.md) | BM25 in Postgres, no extension | Accepted (milestone 3) |
| [021](021-calibration-per-index-and-scorer.md) | Calibration per index and scorer | Accepted (milestone 3) |
| [022](022-eval-runner-and-regression-gate.md) | Eval runner and regression gate | Accepted (milestone 3) |
| [023](023-reindex-beside-the-live-index-then-switch.md) | Re-index beside the live index, then switch | Accepted (milestone 4) |
| [024](024-qdrant-behind-the-store-interface-with-a-postgres-catalog.md) | Qdrant behind the store interface, with a Postgres catalog | Accepted (milestone 4) |
| [025](025-local-models-through-ollamas-native-api.md) | Local models through Ollama's native API | Accepted (milestone 4) |
| [026](026-tracing-and-metrics-through-a-small-facade-on-opentelemetry.md) | Tracing and metrics through a small facade on OpenTelemetry | Accepted (milestone 5) |
| [027](027-langfuse-over-its-otlp-endpoint.md) | Langfuse over its OTLP endpoint | Accepted (milestone 5) |
| [028](028-usage-records-per-operation.md) | Token and cost accounting with a usage record per operation | Accepted (milestone 5) |
| [029](029-langgraph-as-an-optional-orchestrator-adapter.md) | LangGraph as an optional orchestrator adapter | Accepted (milestone 5) |
| [030](030-kubernetes-example-with-kustomize.md) | A Kubernetes example with Kustomize | Accepted (milestone 5) |
