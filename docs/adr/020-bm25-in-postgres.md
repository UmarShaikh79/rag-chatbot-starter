# ADR 020: BM25 in Postgres, no extension

Status: Accepted (milestone 3)

## Decision

Chunks store `tsv = to_tsvector(text_config, text)` and `doc_len` (token count), written in
the same transaction as the embedding (ADR 013), with a GIN index on `tsv`. The pgvector
store's `keyword_search` scores Okapi BM25 (`k1 = 1.2`, `b = 0.75`) in one SQL statement:
candidates are chunks matching any query lexeme; document frequencies and the average length
are counted per query from the index. The text search config (`english` by default) is
recorded with each index, like its embedding model.

## Alternatives considered

- `ts_rank` / `ts_rank_cd`: built in, but not BM25 (no IDF), so rare terms such as error
  codes do not stand out.
- ParadeDB `pg_search` or VectorChord-BM25: real inverted-index BM25 at scale, but an extra
  extension many managed Postgres services do not offer.
- An in-process index (`rank_bm25`, `bm25s`): must be rebuilt from the database and kept in
  sync with re-ingestion in every API process.
- OpenSearch / Elasticsearch: a second datastore to run (ADR 004).

## Why

It works on any Postgres with pgvector, never goes stale (stats are read live), and
re-ingestion updates it atomically with the vectors.

## Consequences

- Per-query statistics cost a count over the index; fine to a few hundred thousand chunks.
  Beyond that, cache the stats or move `keyword_search` to `pg_search` or a search engine:
  the retriever only calls `keyword_search`.
- Changing `vector_store.text_search_config` affects new indexes only; re-ingest into a new
  alias to switch an existing one.
- Migration 0003 backfills existing chunks with the `english` config.
