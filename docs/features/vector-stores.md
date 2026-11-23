# Vector stores

Where passages and their vectors live, and how the nearest ones to a question are found:
**pgvector** inside the Postgres you already run (the default), or **Qdrant** beside it. Both
keep the bookkeeping (aliases, documents, calibrations) in Postgres, so everything else works
the same with either.

Back to the [docs home](../README.md) · See also: [architecture, step 5](../architecture.md#5-one-document-from-file-to-vector)

## At a glance

| | |
| --- | --- |
| **What it does** | Stores each passage with its vector and metadata; answers "the `k` passages closest to this vector, matching these filters"; replaces a document's passages in one step. pgvector also runs BM25 keyword search. |
| **Turned on by** | Always on; `vector_store.backend` (`pgvector` or `qdrant`) in [`config/app.yaml`](../../config/app.yaml) |
| **Main files** | [`stores/pgvector.py`](../../src/chatbot/stores/pgvector.py) (`PgVectorStore`), [`stores/qdrant.py`](../../src/chatbot/stores/qdrant.py) (`QdrantStore`), [`stores/catalog.py`](../../src/chatbot/stores/catalog.py) (`CatalogStore`, shared by both), the `VectorStore`, `KeywordSearch` and `CalibrationStore` Protocols in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | Postgres with pgvector ≥ 0.8 (always: it holds the catalog); a Qdrant server and `uv sync --extra qdrant` for Qdrant |
| **Used by** | [Ingestion](sources-and-parsers.md) (writes), [retrieval](retrieval-strategies.md) (searches), [calibration](weak-retrieval-and-calibration.md) (stores thresholds), [re-indexing](reindexing.md) (aliases) |
| **Decisions** | ADR [004](../adr/004-postgres-for-everything-first.md) (Postgres first), ADR [012](../adr/012-filtered-vector-search-on-pgvector.md) (filtered search), ADR [013](../adr/013-re-ingesting-a-document-is-an-atomic-replace.md) (atomic replace), ADR [020](../adr/020-bm25-in-postgres.md) (BM25 in Postgres), ADR [024](../adr/024-qdrant-behind-the-store-interface-with-a-postgres-catalog.md) (Qdrant with a Postgres catalog) |

## What it does

Finding "passages near this question" among millions by comparing every vector would be too
slow, so stores build an [HNSW](../glossary.md#hnsw) graph: an approximate index that finds
close vectors in a few milliseconds. Closeness is
[cosine similarity](../glossary.md#cosine-similarity): 1 means the same direction, 0 unrelated.
Both stores report it on the same scale, so a threshold means the same in either.

| | pgvector (default) | Qdrant |
| --- | --- | --- |
| Runs in | The Postgres you already have | Its own server (or Qdrant Cloud) |
| Install | Nothing; the Compose image has pgvector 0.8 | `uv sync --extra qdrant`, `make up-qdrant` |
| Keyword search (BM25, hybrid) | Yes (`keyword_search`, in SQL) | No: use `retrieval.strategy: dense` |
| Filters | JSONB containment, exact scan for small slices | Payload filters on filterable HNSW |
| Good for | Up to a few million passages, one database to run | Larger corpora, a dedicated vector service |

The **catalog** is the same for both ([`CatalogStore`](../../src/chatbot/stores/catalog.py)):
Postgres tables for physical indexes and their [aliases](../glossary.md#alias), each
document's content hash and parsed text, and calibrations. That's why
[re-indexing](reindexing.md) can move an index from pgvector to Qdrant and back.

## How it works

The container builds the store with
`registry.create("vector_store", settings.vector_store.backend, settings, pool)`
([`core/container.py`](../../src/chatbot/core/container.py)).

**Writing** (from [`IngestionPipeline.ingest_parsed`](../../src/chatbot/ingestion/pipeline.py)):

1. `ensure_index(alias, model, dims, chunker_version)` resolves the alias to a physical index,
   for example `docs__text_embedding_3_small__1536`, creating it on first use. An alias built
   with another model is refused.
2. `document_state(index, document_id)` returns the stored hash, so unchanged documents are
   skipped.
3. `replace_document(index, document, chunks, vectors)` swaps the document's old passages for
   the new ones. **pgvector**: delete and insert in one transaction. **Qdrant**: one
   `batch_update_points` call (delete, then upsert), then the document row in Postgres.

**Searching** (from [`DenseRetriever.retrieve`](../../src/chatbot/retrieval/dense.py)):
`search(index, vector, k, filters)` returns up to `k` `ScoredChunk`s, best first, with
`score = scores["dense"] =` cosine similarity.

Inside `PgVectorStore.search`:

1. **Build the filter** (`_where`): `index_id = …`, `document_id = ANY(…)` when given, and one
   JSONB containment test per metadata value.
2. **Choose exact or HNSW:** with filters, count matching passages (stopping at
   `exact_scan_threshold` + 1). At most 2,000 match: scan them all exactly, which is cheap and
   always right. Otherwise use the HNSW graph with `hnsw.iterative_scan`, so a selective filter
   still returns `k` rows.
3. **Order by distance** (`<=>`), keep `k`, convert to similarity (`1 − distance`), re-sort
   (`relaxed_order` may return rows slightly out of order).

`QdrantStore.search` sends one `query_points` call with a payload filter and `hnsw_ef`, and
maps each point's payload back to a `Chunk`.

**Keyword search** (pgvector only): `keyword_search(index, query, k, filters)` scores Okapi
[BM25](../glossary.md#bm25) in SQL over a `tsvector` column with a GIN index; see
[retrieval strategies](retrieval-strategies.md#bm25-keyword-search).

### Filters

`filters` come from the chat request (`"filters": {"product": "notes"}`) and mean the same in
every store (`metadata_matches` in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py)):

| Filter | Matches stored metadata |
| --- | --- |
| `{"document_id": "samples:refund-policy.md"}` | that document (a list: any of them) |
| `{"tags": "faq"}` | `"tags": "faq"` and `"tags": ["faq", "billing"]` |
| `{"tags": ["faq", "billing"]}` | only a list holding both |

Several keys are ANDed. Metadata comes from the document (front matter, SQL columns, the
`metadata` of a pushed document) and is copied to each passage.

## Inputs and outputs

**In (search):** an `IndexInfo` (from the alias), the question vector, `k`, and `filters`.

**Out:** a list of `ScoredChunk` ([`core/types.py`](../../src/chatbot/core/types.py)):

| Field | Example (offline profile) |
| --- | --- |
| `chunk` | `id`, `document_id` (`samples:refund-policy.md`), `position`, `text`, `metadata`, `content_hash` |
| `score` | `0.2894`, cosine similarity |
| `retriever` | `dense` (or `bm25` from `keyword_search`) |
| `scores` | `{"dense": 0.2894}`; later steps add `bm25`, `rrf`, `rerank` |

`chatbot index list` shows every physical index (real output, trimmed):

```json
{
  "name": "docs_offline__hashing_v1__384",
  "backend": "pgvector",
  "embedding_model": "hashing-v1",
  "dims": 384,
  "chunker_version": "recursive-v1:512:64:approx",
  "status": "ready",
  "aliases": ["docs_offline"],
  "documents": 8
}
```

## Turn it on, change it, turn it off

pgvector is on by default and needs nothing beyond `DATABASE_URL`. Its settings
([configuration: `vector_store`](../configuration.md#vector_store)):

| Setting | Default | Notes |
| --- | --- | --- |
| `vector_store.index` | `docs` | The alias the app serves (`docs_offline`, `docs_local` in those profiles). |
| `vector_store.hnsw.ef_search` | 100 | Search beam width: higher is more accurate and slower. |
| `vector_store.hnsw.iterative_scan` | `relaxed_order` | Keep it, or selective filters return fewer than `k` rows. |
| `vector_store.hnsw.m` / `ef_construction` | 16 / 64 | Graph build settings; apply to new indexes. |
| `vector_store.exact_scan_threshold` | 2000 | Filters matching at most this many passages scan exactly. |
| `vector_store.text_search_config` | `english` | Postgres language for BM25; fixed per index. |

Tuning at scale (vacuum, rebuilding HNSW after churn, memory) is in
[operating pgvector](../operations/pgvector.md).

**Switching to Qdrant:**

```bash
uv sync --extra qdrant
make up-qdrant                                      # Qdrant in Docker, app built with the extra
CHATBOT__VECTOR_STORE__BACKEND=qdrant chatbot reindex --activate
```

Then set `CHATBOT__VECTOR_STORE__BACKEND=qdrant` (and `QDRANT_URL`, `QDRANT_API_KEY` for a
remote server) for the API and workers, and `retrieval.strategy: dense`. Qdrant settings live
under `vector_store.qdrant` (`hnsw_ef`, `on_disk`, `upsert_batch`, …). The full move is in
[operations: re-indexing](../operations/reindexing.md#moving-between-pgvector-and-qdrant).

**Removing Qdrant:** re-index back to pgvector, `chatbot index drop` the Qdrant index, and
remove the extra (`uv sync` without `--extra qdrant`). Postgres can't be removed: it holds the
catalog, jobs, memory and usage.

## Extend it

Subclass `CatalogStore` and you get aliases, document bookkeeping, calibrations, re-indexing
and rollback for free. You add only the vector parts:

```python
# src/chatbot/domain/my_store.py
from chatbot.core.registry import registry
from chatbot.stores.catalog import CatalogStore


class MyStore(CatalogStore):
    backend = "mystore"

    async def _create_physical(self, info, conn): ...  # a collection for this index
    async def _drop_physical(self, info, conn): ...  # delete it
    async def replace_document(self, index, document, chunks, vectors, user_id=None): ...
    async def delete_document(self, index, document_id): ...
    async def search(self, index, vector, k, filters): ...  # -> list[ScoredChunk], best first


@registry.register("vector_store", "mystore")
def make_my_store(settings, pool):
    return MyStore(pool, settings.vector_store.text_search_config)
```

Select it with `vector_store.backend: mystore`; [`stores/qdrant.py`](../../src/chatbot/stores/qdrant.py)
is the worked example. Rules:

- Apply metadata filters exactly as `metadata_matches` does, and add your store to
  [`test_store_filters.py`](../../tests/integration/test_store_filters.py), which runs the same
  cases against every store.
- Return cosine similarity (higher is better) in both `score` and `scores["dense"]`.
- Write the document row in Postgres only after your vectors are stored (call
  `self._upsert_document`, as `QdrantStore.replace_document` does), so a failure is retried.
- Optional: `keyword_search` (enables BM25 and hybrid), `forget_user_documents` (when vectors
  carry user data outside Postgres, so [forgetting a user](../api.md#memory) leaves nothing).

To use a dedicated keyword engine (ParadeDB `pg_search`, OpenSearch), subclass `PgVectorStore`
and override only `keyword_search`. More in
[extending: add a vector store](../extending.md#add-a-vector-store).

## Test it

```bash
uv run pytest tests/unit          # the in-memory store, no database
make test-db                      # pgvector and Qdrant against the Compose services
```

| Test file | Covers |
| --- | --- |
| [`test_pgvector.py`](../../tests/integration/test_pgvector.py) | model recorded and enforced, atomic replace, a failed replace keeps the old version, selective filters with HNSW and exact scan, BM25, calibrations per scorer |
| [`test_qdrant.py`](../../tests/integration/test_qdrant.py) | collections per index, no stale points after replace, filters and payload, forgetting a user, moving an index from pgvector to Qdrant |
| [`test_store_filters.py`](../../tests/integration/test_store_filters.py) | the same filter cases against the in-memory store, pgvector (exact and HNSW) and Qdrant |

`InMemoryVectorStore` in [`testing/fakes.py`](../../src/chatbot/testing/fakes.py) implements the
same Protocols for unit tests.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `type "vector" does not exist` on `chatbot migrate` | Postgres without pgvector | Use the Compose `postgres` image, or `CREATE EXTENSION vector` (pgvector ≥ 0.8). |
| A filtered question returns fewer passages than `top_k` | `iterative_scan: off`, or `max_scan_tuples` too low for a very selective filter | Keep `relaxed_order`; raise `max_scan_tuples` or `exact_scan_threshold`. |
| `vector_store.backend is qdrant but qdrant-client is not installed` | Missing extra | `uv sync --extra qdrant` (with Docker: `make up-qdrant`). |
| `retrieval uses bm25 but the vector store QdrantStore has no keyword_search` | BM25 or hybrid with Qdrant | `retrieval.strategy: dense`, or stay on pgvector. |
| 409 "Index alias 'docs' points at …, which lives in the pgvector store, but vector_store.backend is qdrant" | The backend changed but the alias still points at the old store's index | `chatbot reindex` with the new backend, then `chatbot index activate`. |
| Search got slower over time on pgvector | Dead tuples and HNSW churn from re-ingestion | See [operating pgvector](../operations/pgvector.md#churn-vacuum-and-hnsw). |

## Related

- Before: [embeddings](embeddings.md) (where vectors come from).
- After: [retrieval strategies](retrieval-strategies.md) (how searches are combined).
- Moving between stores and models: [re-indexing and aliases](reindexing.md).
- Operating at scale: [operating pgvector](../operations/pgvector.md).
- Terms: [HNSW](../glossary.md#hnsw), [index](../glossary.md#index), [alias](../glossary.md#alias),
  [cosine similarity](../glossary.md#cosine-similarity).
