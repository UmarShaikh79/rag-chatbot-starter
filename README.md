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
```

## What is included

| Area | Default | Switch with |
| --- | --- | --- |
| LLM | OpenAI, by profile name (`answer`, `cheap`) | `config/models.yaml`, `models.*` |
| Embeddings | OpenAI `text-embedding-3-small` (1536) | `models.embed` |
| Vector store | Postgres + pgvector 0.8 (HNSW, iterative scan for filters) | `vector_store.*` |
| Ingestion | Files (Markdown, text, HTML, PDF, DOCX) and inline documents via API; Postgres job queue | `ingestion.*` |
| Chunking | Structure-aware recursive, 512 tokens, 64 overlap | `ingestion.chunker.*` |
| Retrieval | Dense, `top_k: 8`; BM25, hybrid (RRF), HyDE, query rewriting and rerank (Cohere or local cross-encoder) one setting away | `retrieval.*` |
| Thresholds | `min_score` + `min_hits`, calibrated per index and pipeline on the golden set | `chatbot calibrate` |
| Evals | Golden set: hit@k, MRR, answer rate, faithfulness, abstention; CI gate with baseline | `chatbot eval`, `evals/gate.yaml` |
| Weak retrieval | Clarifying question, never an invented answer | `answer.on_weak_retrieval` |
| Streaming | `draft_then_final` (deltas, then one terminal event) | `answer.stream_mode: buffered` |
| Safety | Injection checks (normalised heuristics; optional model classifier), PII redaction (regex; optional Presidio), prompt-leak check, all on the final text too | `guardrails.*` |
| Memory | History per caller `user_id`, rolling summaries, long-term user facts; redacted before storage; full per-user deletion | `memory.*` |
| Auth | API keys (hashed), JWT/OIDC bearer tokens, or both | `auth.mode` |
| Observability | JSON logs with a trace ID per request; token counts (and cost, if prices are set) in every response | `observability.*` |

Later milestones add Ollama, Qdrant and re-index with alias flip (M4), and OpenTelemetry, Langfuse, LangGraph and Kubernetes (M5). See [docs/roadmap.md](docs/roadmap.md).

## Quickstart (Docker)

```bash
cp .env.example .env
make api-key                 # prints a key for clients and a hash for .env
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
make test-unit       # ~160 fast tests, no database
make test-db         # integration + end-to-end against Postgres (TEST_DATABASE_URL)
make lint
```

The e2e suite runs the real path (API, Postgres job, worker, pgvector, chat) with the offline
profile. `tests/e2e/test_live_openai.py` runs against OpenAI when `OPENAI_API_KEY` and
`RUN_LIVE_TESTS=1` are set. CI runs lint and all non-live tests against a pgvector service.

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

## Docs

- [Configuration reference](docs/configuration.md)
- [HTTP API and streaming contract](docs/api.md)
- [Extending: add a source, parser, retriever, detector, guard or provider](docs/extending.md)
- [Roadmap](docs/roadmap.md)
- [Architecture decision records](docs/adr/README.md)

## License

MIT. The sample documents describe a fictional product and were written for this repository.
