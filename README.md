# RAG Chatbot Starter

A clone-and-own template for a retrieval-augmented chatbot **backend**. It ingests your
documents, answers questions with citations, asks a clarifying question when it cannot find
the answer, and exposes everything over HTTP and Server-Sent Events so any app (React,
Laravel, .NET, Java, Shopify, ...) can use it.

It is a starting point, not a framework: copy it, delete what you don't need, and change the
code directly. Every part sits behind a small interface and is chosen by config.

```
config/      app.yaml, models.yaml, profiles/      prompts/   versioned prompt templates
src/chatbot/ api, auth, core, providers, stores, ingestion, retrieval, guardrails,
             memory, orchestration, observability, domain (your code)
migrations/  Alembic (raw SQL)     samples/docs/  demo corpus     evals/  golden questions
tests/       unit, integration (Postgres), e2e   docker/  Dockerfile + Compose   docs/  guides + ADRs
deploy/k8s/  Kubernetes example (Kustomize)
```

## What is included

| Area | Default | Switch with |
| --- | --- | --- |
| LLM | OpenAI, by profile name (`answer`, `cheap`); local models through Ollama | `config/models.yaml`, `models.*`, `CHATBOT_PROFILE=local-ollama` |
| Embeddings | OpenAI `text-embedding-3-small` (1536); Ollama `nomic-embed-text` (768) locally | `models.embed` |
| Vector store | Postgres + pgvector 0.8 (HNSW, iterative scan for filters); Qdrant | `vector_store.backend` |
| Re-indexing | New embedder, chunker or store: build beside the live index, switch the alias, roll back | `chatbot reindex`, `chatbot index` |
| Ingestion | Files (Markdown, text, HTML, PDF, DOCX), rows from any SQL database, inline documents via API; Postgres job queue | `ingestion.*` |
| Chunking | Structure-aware recursive, 512 tokens, 64 overlap | `ingestion.chunker.*` |
| Retrieval | Dense, `top_k: 8`; BM25, hybrid (RRF), HyDE, query rewriting and rerank (Cohere or local cross-encoder) one setting away | `retrieval.*` |
| Thresholds | `min_score` + `min_hits`, calibrated per index and pipeline on the golden set | `chatbot calibrate` |
| Evals | Golden set: hit@k, MRR, answer rate, faithfulness, abstention; CI gate with baseline | `chatbot eval`, `evals/gate.yaml` |
| Weak retrieval | Clarifying question, never an invented answer | `answer.on_weak_retrieval` |
| Streaming | `draft_then_final` (deltas, then one terminal event) | `answer.stream_mode: buffered` |
| Safety | Injection checks (normalised heuristics; optional model classifier), PII redaction (regex; optional Presidio), prompt-leak check, all on the final text too | `guardrails.*` |
| Memory | History per caller `user_id`, rolling summaries, long-term user facts; redacted before storage; full per-user deletion | `memory.*` |
| Auth | API keys (hashed), JWT/OIDC bearer tokens, or both | `auth.mode` |
| Observability | JSON logs with a trace ID per request; OpenTelemetry traces and metrics, Langfuse, both off until you turn them on | `observability.otel`, `observability.langfuse` |
| Cost accounting | Tokens and cost per response, a usage record per turn and job, reports by day, user, model | `price_per_mtok`, `chatbot usage`, `GET /v1/usage` |
| Orchestration | Plain async Python, one method per step; the same steps as a LangGraph graph | `orchestrator: langgraph` |
| Deployment | Docker Compose; a Kubernetes example (Kustomize, dev and prod overlays) | `deploy/k8s` |

Optional parts are `uv` extras, so the base install stays small: `otel`, `langfuse`,
`langgraph`, `qdrant`, `rerank`, `presidio`. See [docs/roadmap.md](docs/roadmap.md) for what
is still open.

## Prerequisites

Docker with Compose for the quickstart. For running or testing outside Docker:
Python 3.12+, [uv](https://docs.astral.sh/uv/), and Postgres with pgvector >= 0.8 (the
Compose file starts one on port 5432).

## Quickstart (Docker)

```bash
cp .env.example .env
make api-key                 # prints a key for clients and a hash for .env
                             # (no uv? docker compose -f docker/compose.yaml run --rm --no-deps api chatbot hash-key)
# In .env: set CHATBOT_API_KEYS=<hash> and OPENAI_API_KEY=sk-...
#          (or CHATBOT_PROFILE=offline to run with no API key)
make up                      # postgres + migrations + api + worker
make ingest                  # index samples/docs and wait for the job
make ask KEY=cb_... Q="How long is the refund window for annual plans?"
```

```json
{
  "answer": "Annual Plus and Team subscriptions can be refunded in full within 30 days of purchase [1].",
  "citations": [{"marker": 1, "document_id": "samples:refund-policy.md", "title": "Refund policy", "score": 0.61, "...": "..."}],
  "kind": "answer",
  "conversation_id": "…", "trace_id": "…",
  "usage": {"prompt_tokens": 512, "completion_tokens": 31, "embedding_tokens": 12, "cost_usd": null}
}
```

(The response above is illustrative; scores and wording depend on the model.)

Streaming: `make ask-stream KEY=cb_... Q="..."`. API docs: http://localhost:8000/docs.

## Quickstart (local, without Docker for the app)

```bash
uv sync
export DATABASE_URL=postgresql://chatbot:chatbot@localhost:5432/chatbot   # Postgres with pgvector >= 0.8
export CHATBOT_PROFILE=offline CHATBOT__AUTH__MODE=none                   # no keys needed
uv run chatbot migrate
uv run chatbot ingest --wait
uv run chatbot ask "What does sync error E309 mean?"
uv run chatbot serve          # and `uv run chatbot worker` in another terminal
```

The **offline** profile swaps in a hashing embedder and an extractive fake LLM so the whole
pipeline runs without network access. It quotes sentences rather than writing answers, and it
cannot judge relevance well; use it for plumbing, demos and CI, not for quality.

## Tracing, metrics and cost

```bash
echo "CHATBOT__OBSERVABILITY__OTEL__ENABLED=true" >> .env
make up-otel                 # adds an OpenTelemetry Collector and Jaeger
make ask KEY=cb_... Q="..."  # then open http://localhost:16686
make usage BY=model          # tokens and cost per model (fill price_per_mtok in config/models.yaml)
```

Langfuse instead (or as well): `uv sync --extra langfuse`, set `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `CHATBOT__OBSERVABILITY__LANGFUSE__ENABLED=true`. Each chat turn becomes
a trace with its retrieval, guard and model steps, user and session. See
[docs/observability.md](docs/observability.md).

## Kubernetes

```bash
docker build -f docker/Dockerfile -t chatbot:dev . && kind load docker-image chatbot:dev
kubectl apply -k deploy/k8s/overlays/dev       # Postgres inside, offline profile, no keys
```

`deploy/k8s/overlays/prod` is the starting point for a real cluster (managed Postgres, HPA,
ingress for SSE). See [deploy/k8s/README.md](deploy/k8s/README.md).

## Local models (Ollama) and Qdrant

Both are optional services in the Compose file, off until you name their profile:

```bash
make up-ollama    # adds Ollama and pulls the models; then CHATBOT_PROFILE=local-ollama in .env
make up-qdrant    # adds Qdrant; then CHATBOT__VECTOR_STORE__BACKEND=qdrant in .env
```

Without Docker: install [Ollama](https://ollama.com), pull the three models named in
`config/models.yaml` (`ollama pull nomic-embed-text`, and so on), and run with
`CHATBOT_PROFILE=local-ollama`. Nothing leaves the
machine. Local scores sit on their own scale, so run `chatbot calibrate --apply` after ingesting.

An index belongs to one embedding model, chunker and store. To switch an existing deployment,
build the new index beside the live one and flip the alias when it is ready:

```bash
chatbot reindex          # with the new config; the API keeps serving the old index
chatbot index activate   # switch (and `chatbot index rollback` to go back)
```

See [docs/operations/reindexing.md](docs/operations/reindexing.md).

## Using it from your app

```bash
# Ask (JSON)
curl -s localhost:8000/v1/chat -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "Where are notes stored?", "user_id": "customer-42", "conversation_id": null}'

# Push records from your own database (e.g. help-center articles), owned by a user if you like
curl -s localhost:8000/v1/ingest -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source": "crm", "documents": [{"id": "42", "title": "Order 42", "text": "Order 42 ships Friday."}]}'

# See what is remembered about a user, then forget everything
# (facts, summaries, turns, conversations, queued jobs, documents they own)
curl -s localhost:8000/v1/users/customer-42/memory -H "X-API-Key: $KEY"
curl -s -X DELETE localhost:8000/v1/users/customer-42/memory -H "X-API-Key: $KEY"
```

Endpoints and the SSE event contract: [docs/api.md](docs/api.md).

## Tests

```bash
make test-unit       # ~210 fast tests, no database (`uv sync --extra otel --extra langgraph` to run them all)
make test-db         # integration + end-to-end against Postgres (TEST_DATABASE_URL)
make lint
```

The e2e suite runs the real path (API, Postgres job, worker, pgvector, chat) with the offline
profile. `tests/e2e/test_live_openai.py` runs against OpenAI when `OPENAI_API_KEY` and
`RUN_LIVE_TESTS=1` are set, and `tests/e2e/test_live_ollama.py` against Ollama when
`OLLAMA_TEST_URL` is set. Qdrant tests run Qdrant's in-process mode (`uv sync --extra qdrant`),
or a server when `QDRANT_TEST_URL` is set. CI runs lint and all non-live tests against pgvector
and Qdrant services.

## Evals and calibration

```bash
make calibrate-apply   # sweep min_score + min_hits on the golden set, store them with the index
make eval              # score retrieval and answers, check evals/gate.yaml
make eval-retrieval    # retrieval metrics only, no LLM calls
```

Run both after ingesting, and again whenever you change the embedder, retrieval strategy,
HyDE or reranker. CI does this with the offline profile on every pull request and fails below
the floors in `evals/gate.yaml` or on a drop from `evals/baselines/offline.json`. See
[evals/README.md](evals/README.md).

## Make it yours

After cloning, the usual first steps:

1. Put your documents in a source: replace `samples/docs`, or add a `sql` source or your own
   (`ingestion.sources` in `config/app.yaml`, [extending](docs/extending.md#add-a-source)).
2. Pick models in `config/models.yaml` and fill `price_per_mtok` so costs are reported.
3. Ingest, then `make calibrate-apply` and `make eval`: thresholds are per index and model.
4. Replace `evals/datasets/golden.jsonl` with questions about your documents; the CI gate
   then protects your answers, not the samples.
5. Edit the prompts in `prompts/` (copy to a new version) and the persona in them.
6. Delete what you will not use: most optional modules are a file or folder plus a line in
   `BUILTIN_MODULES` (`src/chatbot/core/container.py`) and its tests.

## Docs

- [Configuration reference](docs/configuration.md)
- [HTTP API and streaming contract](docs/api.md)
- [Extending: add a source, parser, retriever, detector, guard, provider, or change the flow](docs/extending.md)
- [Observability: traces, metrics, Langfuse, cost accounting](docs/observability.md)
- [Kubernetes example](deploy/k8s/README.md)
- [Re-indexing: new embedding model, chunker or store](docs/operations/reindexing.md)
- [Operating pgvector: tuning, autovacuum, HNSW rebuilds](docs/operations/pgvector.md)
- [Roadmap](docs/roadmap.md)
- [Architecture decision records](docs/adr/README.md)

## License

MIT. The sample documents describe a fictional product and were written for this repository.
