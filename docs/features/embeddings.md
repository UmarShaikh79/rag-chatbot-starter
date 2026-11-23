# Embeddings

How text becomes the vectors that [dense retrieval](../glossary.md#dense-retrieval) compares:
the embedding model turns every passage at ingest time, and every question at chat time, into
a list of numbers. Questions and the passages that answer them end up close together.

Back to the [docs home](../README.md) · See also: [architecture, step 5](../architecture.md#5-one-document-from-file-to-vector)

## At a glance

| | |
| --- | --- |
| **What it does** | Turns passages (`embed_documents`) and questions (`embed_query`) into vectors of a fixed size, and reports the tokens it used. |
| **Turned on by** | Always on; `models.embed` in [`config/app.yaml`](../../config/app.yaml) names a profile under `embeddings:` in [`config/models.yaml`](../../config/models.yaml) |
| **Main files** | [`providers/embeddings/openai.py`](../../src/chatbot/providers/embeddings/openai.py) (`OpenAIEmbedder`), [`providers/embeddings/ollama.py`](../../src/chatbot/providers/embeddings/ollama.py) (`OllamaEmbedder`), [`providers/embeddings/hashing.py`](../../src/chatbot/providers/embeddings/hashing.py) (`HashingEmbedder`), the `Embedder` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | An API key (OpenAI), a running Ollama, or nothing (hashing) |
| **Used by** | [`IngestionPipeline.ingest_parsed`](../../src/chatbot/ingestion/pipeline.py) (passages), [`DenseRetriever.retrieve`](../../src/chatbot/retrieval/dense.py) (questions) |
| **Decisions** | ADR [005](../adr/005-the-embedding-model-is-part-of-the-index.md) (the model is part of the index), ADR [009](../adr/009-hosted-and-local-defaults.md) (hosted and local defaults), ADR [025](../adr/025-local-models-through-ollamas-native-api.md) (Ollama's native API) |

## What it does

An [embedding](../glossary.md#embedding) is a list of numbers (1,536 of them for OpenAI's
`text-embedding-3-small`) that stands for the meaning of a text. Two texts about the same thing
get vectors that point in nearly the same direction, even when they share no words: "Can I get
my money back?" lands near a passage titled "Refund policy". Dense retrieval relies on exactly
that.

Three embedders are built in:

| Profile (in `config/models.yaml`) | Provider | Dims | Used by | Notes |
| --- | --- | --- | --- | --- |
| `openai:text-embedding-3-small` | `openai` | 1536 | `dev`, `prod` (the default) | Needs `OPENAI_API_KEY`. Priced at `price_per_mtok.input`. |
| `ollama:nomic-embed-text` | `ollama` | 768 | `local-ollama` | Local, no key. Uses the task prefixes the model was trained with. |
| `hashing:384` | `hashing` | 384 | `offline`, tests, CI | Hashed word counts. Matches shared **words**, not meaning. Never for production. |

**One index, one model.** Vectors from two models live in different spaces and can't be
compared, so an index records the model and size it was built with, and the code refuses to
search it with another ([re-indexing](reindexing.md)).

## How it works

**At ingest time**, [`IngestionPipeline.ingest_parsed`](../../src/chatbot/ingestion/pipeline.py)
chunks a document, then calls `embedder.embed_documents(batch)` for every
`ingestion.embed_batch_size` (64) passages and stores each vector with its passage.

**At chat time**, [`DenseRetriever.retrieve`](../../src/chatbot/retrieval/dense.py) first checks
the index (`check_index`: same `model_id` and `dims` as the configured embedder, or a 409), then
calls `embedder.embed_query(question)` and hands the vector to the
[vector store](vector-stores.md).

How the container builds it ([`core/container.py`](../../src/chatbot/core/container.py)):

1. `make_embedder(settings)` reads the profile named by `models.embed` and calls
   `registry.create("embedder", profile.provider, profile)`.
2. `metered_embedder` wraps it in `MeteredEmbedder`
   ([`observability/instrument.py`](../../src/chatbot/observability/instrument.py)), which adds
   an `embeddings <model>` span, token metrics and the cost from `price_per_mtok`. Your own
   embedder gets this for free.

What each provider does inside:

- **OpenAI**: one `client.embeddings.create(model, input, dimensions=dims)` call per batch. A
  missing key stops startup with a one-line message, not a traceback.
- **Ollama**: `POST /api/embed` with all texts at once. Adds `document_prefix` to passages and
  `query_prefix` to questions (`search_document: ` and `search_query: ` for
  `nomic-embed-text`), checks that every vector has the profile's `dims`, and counts tokens
  from `prompt_eval_count`.
- **Hashing**: lower-cases the text, drops stopwords, hashes each word and each pair of
  neighbouring words into one of `dims` slots (+1 or −1), then scales the vector to length 1.
  Tokens are estimated as characters / 4.

## Inputs and outputs

**In:** a list of passage texts (`embed_documents`) or one question (`embed_query`).

**Out:** the vectors, plus a `Usage` with `embedding_tokens`:

```python
vectors, usage = await embedder.embed_documents(["Refund policy > Annual plans\nAnnual Plus ..."])
len(vectors[0])  # 1536 for text-embedding-3-small
usage.embedding_tokens  # what the provider reported
vector, usage = await embedder.embed_query("How long is the refund window?")
```

Every embedder also exposes `model_id` and `dims`. Both are stored with the index; the
[chat response](../api.md#post-v1chat) reports the tokens under `usage.embedding_tokens`.

## Turn it on, change it, turn it off

Embeddings are always on. Pick the profile in `config/app.yaml` (or a profile file, or an
environment variable):

```bash
CHATBOT__MODELS__EMBED=ollama:nomic-embed-text   # any name under embeddings: in config/models.yaml
```

A profile's fields ([configuration: models](../configuration.md#models-and-configmodelsyaml)):

| Field | Example | Notes |
| --- | --- | --- |
| `provider` | `openai` | The registered embedder: `openai`, `ollama`, `hashing`, or yours. |
| `model` | `text-embedding-3-small` | The provider's model ID. Stored with the index. |
| `dims` | `1536` | Vector size. OpenAI is asked for exactly this many; Ollama is checked against it. |
| `api_key_env` | `OPENAI_API_KEY` | The variable holding the key, never the key itself. |
| `base_url` | `null` | Another endpoint: Azure OpenAI or any OpenAI-compatible server, a remote Ollama. |
| `params` | `{document_prefix: "search_document: "}` | Provider options. Ollama: `document_prefix`, `query_prefix`, `keep_alive`, `truncate`, `timeout_s`. |
| `price_per_mtok` | `{input: 0.02}` | USD per million tokens, for [usage and cost](../observability.md#cost-and-token-accounting). |

**Changing the model, its `dims` or its prefixes** makes every stored vector incomparable with
new questions. Build a new index beside the live one and switch:

```bash
CHATBOT__MODELS__EMBED=ollama:nomic-embed-text chatbot reindex
```

The whole procedure, with checks and rollback, is in [re-indexing](reindexing.md). Recalibrate
afterwards: each model has its own score scale ([calibration](weak-retrieval-and-calibration.md)).

**Turning it off:** dense retrieval needs it. With `retrieval.strategy: bm25` no question is
embedded, but passages still are at ingest time, and the index still belongs to the model.
For no network at all, use the `hashing:384` profile (`CHATBOT_PROFILE=offline`).

**Removing a provider you don't use:** delete its profile from `config/models.yaml`. The
provider modules have no extra dependencies (the OpenAI SDK is a core dependency; Ollama uses
`httpx`), so there is nothing to uninstall.

## Extend it

An embedder is anything with `model_id`, `dims`, `embed_documents()` and `embed_query()` (the
`Embedder` Protocol). Register a factory that takes the profile:

```python
# src/chatbot/domain/voyage_embedder.py
import os

import httpx

from chatbot.core.registry import registry
from chatbot.core.types import Usage


class VoyageEmbedder:
    def __init__(self, profile):
        self.model_id = profile.model
        self.dims = profile.dims
        self.key = os.environ[profile.api_key_env or "VOYAGE_API_KEY"]

    async def _embed(self, texts, input_type):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model_id, "input": texts, "input_type": input_type},
            )
        resp.raise_for_status()  # an httpx error becomes a 502 for the client
        data = resp.json()
        vectors = [item["embedding"] for item in data["data"]]
        return vectors, Usage(embedding_tokens=data["usage"]["total_tokens"])

    async def embed_documents(self, texts):
        return await self._embed(texts, "document") if texts else ([], Usage())

    async def embed_query(self, text):
        vectors, usage = await self._embed([text], "query")
        return vectors[0], usage


@registry.register("embedder", "voyage")
def make_voyage(profile):
    return VoyageEmbedder(profile)
```

Import it from `src/chatbot/domain/__init__.py`, add a profile with `provider: voyage`, `model`
and `dims` to `config/models.yaml`, point `models.embed` at it, and re-index. Rules worth
keeping from the built-in ones:

- Return vectors in the same order as the texts.
- Return exactly `dims` numbers per vector; the store rejects any other size.
- If the model wants different handling for questions and passages (prefixes, an
  `input_type`), apply it in `embed_query` versus `embed_documents`.
- Any OpenAI-compatible embeddings server (vLLM, LM Studio, Azure OpenAI) needs no code:
  `provider: openai` with a `base_url`.

More in [extending: add an LLM or embedding provider](../extending.md#add-an-llm-or-embedding-provider).

## Test it

```bash
uv run pytest tests/unit/test_ollama.py tests/unit/test_worker_pipeline.py
```

| Test file | Covers |
| --- | --- |
| [`test_ollama.py`](../../tests/unit/test_ollama.py) | prefixes on passages and questions, the `dims` check, unknown `params` refused, the `local-ollama` profile builds Ollama parts |
| [`test_worker_pipeline.py`](../../tests/unit/test_worker_pipeline.py) | unchanged documents are not re-embedded |
| [`test_pgvector.py`](../../tests/integration/test_pgvector.py), [`test_qdrant.py`](../../tests/integration/test_qdrant.py) | an index records its model and refuses another (`make test-db`) |
| [`test_live_openai.py`](../../tests/e2e/test_live_openai.py), [`test_live_ollama.py`](../../tests/e2e/test_live_ollama.py) | the real providers, skipped unless a key or server is set |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Set OPENAI_API_KEY for the OpenAI model text-embedding-3-small` at startup | No key in the environment | Put the key in `.env`, or `CHATBOT_PROFILE=offline`. |
| API answers 409 "Index 'docs' was built with ..." | `models.embed` differs from the model the index was built with | [Re-index](reindexing.md), or switch `models.embed` back. |
| `nomic-embed-text returned 768 dims but its profile says 1024` | `dims` in the profile doesn't match the model | Fix `dims` in `config/models.yaml` (before the index is built). |
| Ollama: connection refused or "model not found" | Ollama not running, or the model not pulled | `ollama serve`; `ollama pull nomic-embed-text` (or `make up-ollama`). |
| Offline profile answers questions it shouldn't | The hashing embedder matches words, not meaning | Expected; use a real model for real quality. |
| Retrieval got worse after changing prefixes | Passages were embedded with the old prefixes | Prefixes are part of the vectors: re-index. |

## Related

- Where vectors go next: [vector stores](vector-stores.md), then
  [retrieval strategies](retrieval-strategies.md).
- Changing the model safely: [re-indexing and aliases](reindexing.md).
- Before: [chunking](chunking.md) decides what each vector stands for.
- Terms: [embedding](../glossary.md#embedding), [cosine similarity](../glossary.md#cosine-similarity),
  [index](../glossary.md#index), [profile](../glossary.md#profile).
