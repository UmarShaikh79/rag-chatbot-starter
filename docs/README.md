# Documentation

Everything about building with, changing and running RAG Chatbot Starter. Start where you are:

| You are... | Read, in this order |
| --- | --- |
| **New to RAG or to this repo** | [Architecture](architecture.md) → [Glossary](glossary.md) → [Tutorial: add your own retriever](tutorial.md) |
| **Building on it** | The [feature guide](#feature-guides) for the part you're changing → [Extending](extending.md) → [Configuration](configuration.md) |
| **Integrating an app** | [HTTP API and streaming](api.md) → [Configuration: `auth` and `api`](configuration.md#auth) |
| **Running it in production** | [Observability](observability.md) → [Re-indexing](operations/reindexing.md) → [Operating pgvector](operations/pgvector.md) → [Kubernetes](../deploy/k8s/README.md) |
| **Asking "why is it like this?"** | [Architecture decision records](adr/README.md) |

## Start here

| Guide | What you get from it |
| --- | --- |
| [Architecture](architecture.md) | The three ideas the code rests on (Protocols, the registry, the container), then one chat request and one document followed through the code, function by function. |
| [Glossary](glossary.md) | Every term (embedding, BM25, RRF, calibration, gate score, ...) in plain words, with where it lives in the code. |
| [Tutorial](tutorial.md) | Add a retriever of your own, register it, configure it, calibrate it, evaluate it. Its code is run by the test suite, so it always works. |

## Feature guides

One guide per feature, each standing on its own. They share the same sections, so you always
know where to look:

1. **At a glance**: what it does, what turns it on, the main files, what it depends on.
2. **What it does**: in plain words.
3. **How it works**: the path through the code, from the function that takes the input to the
   one that produces the output.
4. **Inputs and outputs**: the types going in and out.
5. **Turn it on, change it, turn it off**: settings, installs, and what to delete to remove it.
6. **Extend it**: the Protocol to implement, with an example.
7. **Test it**: the commands and test files.
8. **Common problems**: symptom, cause, fix.
9. **Related**: the guides, settings and decisions it connects to.

**Getting data in**

| Guide | Covers |
| --- | --- |
| [Sources and parsers](features/sources-and-parsers.md) | Files, SQL rows and API-pushed documents; Markdown, HTML, PDF and DOCX parsing; sync and removal. |
| [Chunking](features/chunking.md) | How documents are split into passages, sizes and overlap, the chunker version. |
| [Background jobs and the worker](features/background-jobs.md) | The Postgres job queue, the worker, retries, job kinds, running jobs from the CLI. |
| [Re-indexing and aliases](features/reindexing.md) | Changing the embedding model, chunker or store safely; aliases, switching and rolling back. |

Coming next, in this order: **Finding answers** (embeddings, vector stores, retrieval
strategies, reranking, weak retrieval and calibration), **Answering** (models and providers,
prompts, answers and streaming, orchestration), **Safety and users** (guardrails, memory,
authentication) and **Running it** (the HTTP API, observability, usage and cost, evals,
deployment). Until then, [configuration](configuration.md) and [extending](extending.md) cover
those features.

## Reference

| Doc | Covers |
| --- | --- |
| [Configuration](configuration.md) | Every setting, its default and what it does, section by section. |
| [HTTP API](api.md) | Endpoints, request and response fields, the streaming contract, errors. |
| [Extending](extending.md) | A short recipe for each kind of part you can add. |
| [Observability](observability.md) | Trace IDs, logs, OpenTelemetry, Langfuse, metrics, cost accounting. |
| [Re-indexing](operations/reindexing.md) | The operations runbook for moving to a new model, chunker or store. |
| [Operating pgvector](operations/pgvector.md) | Tuning, vacuum, HNSW rebuilds. |
| [Kubernetes](../deploy/k8s/README.md) | The Kustomize example, dev and prod. |
| [Evals](../evals/README.md) | The golden set, metrics, calibration and the CI gate. |
| [Architecture decision records](adr/README.md) | Why each design choice was made, and what was rejected. |
| [Roadmap](roadmap.md) | What is planned. |

## Conventions in these docs

- Links to code point at the file; the function or class is named next to it, so a link stays
  right when lines move.
- A term in *italics* links to the [glossary](glossary.md).
- `config/app.yaml` paths are written as `section.key` (`retrieval.top_k`); the same setting
  as an environment variable is `CHATBOT__RETRIEVAL__TOP_K`.
- Every link in `docs/` is checked by the test suite
  ([`tests/unit/test_docs_links.py`](../tests/unit/test_docs_links.py)), so a renamed file or
  heading fails CI instead of leaving a dead link.
